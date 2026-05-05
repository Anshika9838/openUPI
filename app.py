import hashlib
import os
import secrets
from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for
from flask_socketio import SocketIO, disconnect, emit, join_room
from parser import extract_utr, parse_upi_message

try:
    from supabase import create_client
except Exception:  # pragma: no cover - fallback if dependency is missing
    create_client = None


load_dotenv()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def make_order_id() -> str:
    return f"ORD-{datetime.now(timezone.utc):%Y%m%d}-{secrets.token_hex(3).upper()}"


def make_txn_id() -> str:
    return f"TXN-{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{secrets.token_hex(2).upper()}"


app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.getenv("FLASK_SECRET_KEY", secrets.token_urlsafe(32)),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE=os.getenv("SESSION_COOKIE_SAMESITE", "Lax"),
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true",
    PERMANENT_SESSION_LIFETIME=timedelta(
        minutes=int(os.getenv("SESSION_LIFETIME_MINUTES", "30"))
    ),
    MAX_CONTENT_LENGTH=1024 * 1024,
)

_socket_origin_defaults = {
    "http://localhost:5000",
    "http://127.0.0.1:5000",
    "http://localhost",
    "http://127.0.0.1",
}
_socket_origin_env = {
    item.strip()
    for item in (os.getenv("SOCKET_CORS_ALLOWED_ORIGINS") or "").split(",")
    if item.strip()
}

socketio = SocketIO(
    app,
    async_mode="threading",
    cors_allowed_origins=sorted(_socket_origin_defaults | _socket_origin_env),
    logger=False,
    engineio_logger=False,
)


def _is_allowed_cors_origin(origin: str | None) -> bool:
    if not origin:
        return False

    allowed_env = os.getenv("API_CORS_ALLOWED_ORIGINS", "")
    allowed_origins = {item.strip() for item in allowed_env.split(",") if item.strip()}
    demo_defaults = {
        "null",
        "http://localhost",
        "http://127.0.0.1",
        "http://localhost:5000",
        "http://127.0.0.1:5000",
    }

    return origin in demo_defaults or origin in allowed_origins


@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin")
    if request.path.startswith("/api/") and _is_allowed_cors_origin(origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Webhook-Secret, X-API-Key"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


class OrderRepository:
    def __init__(self) -> None:
        self._memory: dict[str, dict] = {}
        self._supabase = None
        self._table = os.getenv("SUPABASE_TABLE", "checkout_orders")

        supabase_url = os.getenv("SUPABASE_URL")
        supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv(
            "SUPABASE_ANON_KEY"
        )

        if create_client and supabase_url and supabase_key:
            try:
                self._supabase = create_client(supabase_url, supabase_key)
            except Exception:
                self._supabase = None

    @property
    def using_supabase(self) -> bool:
        return self._supabase is not None

    def create(self, order: dict) -> dict:
        record = dict(order)
        record.setdefault("verification_status", "pending")
        record.setdefault("status_message", "Awaiting payment confirmation")
        record.setdefault("created_at", utc_now_iso())
        record.setdefault("updated_at", utc_now_iso())

        if self._supabase:
            try:
                payload = dict(record)
                payload.pop("checkout_token", None)
                self._supabase.table(self._table).insert(payload).execute()
            except Exception:
                pass

        self._memory[record["order_id"]] = record

        return record

    def get(self, order_id: str) -> dict | None:
        cached = self._memory.get(order_id)
        if self._supabase:
            try:
                response = (
                    self._supabase.table(self._table)
                    .select("*")
                    .eq("order_id", order_id)
                    .limit(1)
                    .execute()
                )
                data = response.data or []
                if data:
                    if cached:
                        return {**data[0], **cached}
                    return data[0]
            except Exception:
                pass
        return cached

    def update(self, order_id: str, patch: dict) -> dict | None:
        current = self.get(order_id)
        if not current:
            return None

        updated = {**current, **patch, "updated_at": utc_now_iso()}

        if self._supabase:
            try:
                payload = dict(updated)
                payload.pop("checkout_token", None)
                self._supabase.table(self._table).update(payload).eq("order_id", order_id).execute()
            except Exception:
                pass

        self._memory[order_id] = updated

        return updated

    def validate_checkout_token(self, order_id: str, token: str) -> bool:
        order = self.get(order_id)
        if not order:
            return False

        expected_hash = order.get("checkout_token_hash")
        if expected_hash:
            return secrets.compare_digest(expected_hash, hash_token(token))

        stored_token = order.get("checkout_token")
        if stored_token:
            return secrets.compare_digest(stored_token, token)

        return False


class PaymentRepository:
    def __init__(self) -> None:
        self._memory: dict[str, dict] = {}
        self._supabase = None
        self._table = os.getenv("PAYMENT_RECEIVED_TABLE", "payment_received")

        supabase_url = os.getenv("SUPABASE_URL")
        supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv(
            "SUPABASE_ANON_KEY"
        )

        if create_client and supabase_url and supabase_key:
            try:
                self._supabase = create_client(supabase_url, supabase_key)
            except Exception:
                self._supabase = None

    def upsert_by_utr(self, record: dict) -> dict:
        payload = dict(record)
        payload.setdefault("verification_status", "pending")
        payload.setdefault("created_at", utc_now_iso())
        payload["updated_at"] = utc_now_iso()
        utr = payload["utr"].upper()
        payload["utr"] = utr

        if self._supabase:
            try:
                self._supabase.table(self._table).upsert(payload, on_conflict="utr").execute()
            except Exception:
                pass

        self._memory[utr] = payload
        return payload

    def get_by_utr(self, utr: str) -> dict | None:
        normalized = (utr or "").strip().upper()
        if not normalized:
            return None

        cached = self._memory.get(normalized)
        if self._supabase:
            try:
                response = (
                    self._supabase.table(self._table)
                    .select("*")
                    .eq("utr", normalized)
                    .limit(1)
                    .execute()
                )
                data = response.data or []
                if data:
                    if cached:
                        return {**data[0], **cached}
                    return data[0]
            except Exception:
                pass
        return cached

    def update(self, utr: str, patch: dict) -> dict | None:
        current = self.get_by_utr(utr)
        if not current:
            return None

        updated = {**current, **patch, "updated_at": utc_now_iso()}
        updated["utr"] = (updated.get("utr") or utr).upper()

        if self._supabase:
            try:
                self._supabase.table(self._table).update(updated).eq("utr", updated["utr"]).execute()
            except Exception:
                pass

        self._memory[updated["utr"]] = updated
        return updated


repository = OrderRepository()
payment_repository = PaymentRepository()


def session_authorized(order_id: str) -> bool:
    authorized_orders = session.get("authorized_orders", {})
    return bool(authorized_orders.get(order_id))


def require_checkout_session(view):
    @wraps(view)
    def wrapped(order_id, *args, **kwargs):
        if not session_authorized(order_id):
            token = request.args.get("token")
            if token and repository.validate_checkout_token(order_id, token):
                authorized_orders = session.get("authorized_orders", {})
                authorized_orders[order_id] = utc_now_iso()
                session["authorized_orders"] = authorized_orders
                session["active_order_id"] = order_id
                session.permanent = True
                if request.endpoint in {"checkout", "transaction_result"} and request.method == "GET":
                    return redirect(url_for(request.endpoint, order_id=order_id))
            else:
                abort(403)
        return view(order_id, *args, **kwargs)

    return wrapped


def room_name(order_id: str) -> str:
    return f"order:{order_id}"


def publish_order_update(order: dict) -> None:
    socketio.emit("verification_update", public_order(order), room=room_name(order["order_id"]))


def public_order(order: dict) -> dict:
    safe_order = dict(order)
    safe_order.pop("checkout_token_hash", None)
    safe_order.pop("checkout_token", None)
    safe_order.pop("entered_utr", None)
    safe_order.pop("parsed_utr", None)
    safe_order.pop("parsed_transaction", None)
    safe_order.pop("transaction_description", None)
    safe_order.pop("verification_payload", None)
    return safe_order


def public_payment(payment: dict) -> dict:
    safe_payment = dict(payment)
    safe_payment.pop("parsed_transaction", None)
    safe_payment.pop("transaction_description", None)
    return safe_payment


def store_checkout_snapshot(order: dict) -> None:
    session["checkout_order_snapshot"] = {
        "order_id": order.get("order_id"),
        "txn_id": order.get("txn_id"),
        "amount": order.get("amount"),
        "currency": order.get("currency", "INR"),
        "merchant_name": order.get("merchant_name"),
        "upi_id": order.get("upi_id"),
        "note": order.get("note"),
        "status": order.get("status", "pending"),
        "verification_status": order.get("verification_status", "pending"),
        "status_message": order.get("status_message", "Awaiting payment confirmation"),
        "checkout_token_hash": order.get("checkout_token_hash"),
        "created_at": order.get("created_at"),
        "updated_at": order.get("updated_at"),
        "metadata": order.get("metadata") or {},
    }
    session.modified = True


def get_checkout_order(order_id: str) -> dict | None:
    order = repository.get(order_id)
    if order:
        return order

    snapshot = session.get("checkout_order_snapshot") or {}
    if snapshot.get("order_id") == order_id:
        # Rehydrate the repository so later lookups and updates can proceed.
        repository.create(snapshot)
        order = repository.get(order_id)
        if order:
            return order
        return snapshot

    return None


def transaction_ingest_key() -> str | None:
    return os.getenv("TRANSACTION_INGEST_API_KEY") or os.getenv("WEBHOOK_SHARED_SECRET")


def require_ingest_api_key() -> bool:
    expected = transaction_ingest_key()
    incoming = request.headers.get("X-API-Key") or request.headers.get("X-Webhook-Secret")
    return bool(expected and incoming and secrets.compare_digest(incoming, expected))


def build_payment_uri(order: dict) -> str:
    upi_id = order.get("upi_id") or "merchant@upi"
    merchant_name = order.get("merchant_name") or "PayNow Merchant"
    note = order.get("note") or order["order_id"]
    amount = float(order.get("amount") or 0)
    params = {
        "pa": upi_id,
        "pn": merchant_name,
        "am": f"{amount:.2f}",
        "tn": note,
        "tr": order["order_id"],
        "cu": order.get("currency", "INR"),
    }
    return f"upi://pay?{urlencode(params)}"


@app.route("/")
def home():
    return jsonify({"message": "PayNow checkout service is running", "status_endpoint": url_for("status")})


@app.route("/api/status")
def status():
    return jsonify(
        {
            "status": "online",
            "version": "2.0.0",
            "supabase_enabled": repository.using_supabase,
        }
    )


@app.route("/api/orders", methods=["POST"])
def create_order():
    data = request.get_json(silent=True) or {}

    try:
        amount = float(data.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "amount must be numeric"}), 400

    if amount <= 0:
        return jsonify({"error": "amount must be greater than zero"}), 400

    upi_id = (data.get("upi_id") or "").strip()
    merchant_name = (data.get("merchant_name") or "").strip()
    description = (data.get("description") or data.get("short_description") or data.get("note") or "").strip()

    if not upi_id or not merchant_name or not description:
        return jsonify(
            {
                "error": "upi_id, merchant_name, amount, and description are required",
            }
        ), 400

    order_id = data.get("order_id") or make_order_id()
    checkout_token = secrets.token_urlsafe(24)
    now = utc_now_iso()
    order = {
        "order_id": order_id,
        "txn_id": data.get("txn_id") or make_txn_id(),
        "amount": round(amount, 2),
        "currency": "INR",
        "merchant_name": merchant_name,
        "upi_id": upi_id,
        "note": description,
        "status": "pending",
        "verification_status": "pending",
        "status_message": "Awaiting payment confirmation",
        "checkout_token_hash": hash_token(checkout_token),
        "created_at": now,
        "updated_at": now,
        "metadata": data.get("metadata") or {},
    }

    repository.create(order)

    checkout_url = url_for("checkout", order_id=order_id, token=checkout_token, _external=False)
    return jsonify(
        {
            "message": "Order created",
            "order": public_order(order),
            "checkout_url": checkout_url,
            "checkout_token": checkout_token,
        }
    ), 201


@app.route("/checkout/<order_id>", methods=["GET", "POST"])
@require_checkout_session
def checkout(order_id: str):
    order = get_checkout_order(order_id)
    if not order:
        abort(404)

    payment_uri = build_payment_uri(order)
    session["active_order_id"] = order_id
    session.permanent = True
    store_checkout_snapshot(order)

    return render_template(
        "checkout.html",
        order=public_order(order),
        payment_uri=payment_uri,
        checkout_session_ready=True,
    )


@app.route("/transaction/<order_id>")
@require_checkout_session
def transaction_result(order_id: str):
    order = get_checkout_order(order_id)
    if not order:
        abort(404)
    return render_template("transaction_result.html", order=public_order(order))


@app.route("/api/orders/<order_id>", methods=["GET"])
@require_checkout_session
def get_order(order_id: str):
    order = get_checkout_order(order_id)
    if not order:
        return jsonify({"error": "Order not found"}), 404
    return jsonify(public_order(order))


@app.route("/api/payments/receive", methods=["POST"])
def ingest_transaction_description():
    payload = request.get_json(silent=True) or {}
    description = (
        payload.get("transaction_description")
        or payload.get("description")
        or payload.get("message")
        or payload.get("text")
        or ""
    ).strip()

    if not description:
        return jsonify({"error": "transaction_description is required"}), 400

    if not require_ingest_api_key():
        return jsonify({"error": "Unauthorized"}), 401

    parsed = parse_upi_message(description)
    parsed_utr = parsed.get("utr") or extract_utr(description)

    if not parsed_utr:
        return jsonify({"error": "Could not extract UTR from transaction description"}), 400

    payment = payment_repository.upsert_by_utr(
        {
            "payment_id": payload.get("payment_id") or make_txn_id(),
            "utr": parsed_utr,
            "amount": parsed.get("amount"),
            "currency": parsed.get("cu") or "INR",
            "transaction_description": description,
            "parsed_transaction": parsed,
            "verification_status": "pending",
            "status_message": "Transaction received and stored in payment_received.",
        }
    )

    return jsonify(
        {
            "message": "Transaction description stored",
            "payment_received": public_payment(payment),
            "parsed_utr": parsed_utr,
        }
    )


@app.route("/api/orders/<order_id>/verify-utr", methods=["POST"])
@require_checkout_session
def verify_utr(order_id: str):
    payload = request.get_json(silent=True) or {}
    user_utr = (payload.get("utr") or "").strip().upper()
    customer_name = (payload.get("customer_name") or "").strip()
    customer_email = (payload.get("customer_email") or "").strip()
    customer_mobile = (payload.get("customer_mobile") or "").strip()
    reference_note = (payload.get("reference_note") or "").strip()
    if not user_utr:
        return jsonify({"error": "UTR is required"}), 400

    order = get_checkout_order(order_id)
    if not order:
        return jsonify({"error": "Order not found"}), 404

    if (order.get("verification_status") or "").lower() == "success":
        existing_reference = (order.get("payment_reference") or "").strip().upper()
        if existing_reference and existing_reference != user_utr:
            return jsonify({"error": "Verification failed"}), 409
        if existing_reference == user_utr:
            return jsonify(
                {
                    "message": "Order already verified",
                    "matched": True,
                    "already_verified": True,
                }
            )

    customer_patch = {
        "customer_name": customer_name or order.get("customer_name") or "",
        "customer_email": customer_email or order.get("customer_email") or "",
        "customer_mobile": customer_mobile or order.get("customer_mobile") or "",
        "reference_note": reference_note or order.get("reference_note") or "",
    }

    payment = payment_repository.get_by_utr(user_utr)
    if not payment:
        order = repository.update(
            order_id,
            {
                "entered_utr": user_utr,
                "verification_status": "pending",
                "status": "pending",
                "status_message": "Unidentified UTR",
                **customer_patch,
            },
        )
        publish_order_update(order)
        return jsonify({"error": "Unidentified UTR"}), 404

    existing_verified_order = (payment.get("verified_order_id") or "").strip()
    if existing_verified_order and existing_verified_order != order_id:
        order = repository.update(
            order_id,
            {
                "entered_utr": user_utr,
                "verification_status": "pending",
                "status": "pending",
                "status_message": "UTR already used",
                **customer_patch,
            },
        )
        publish_order_update(order)
        return jsonify({"error": "UTR already used"}), 409

    order_amount = float(order.get("amount") or 0)
    payment_amount = float(payment.get("amount") or 0)
    if round(order_amount, 2) != round(payment_amount, 2):
        order = repository.update(
            order_id,
            {
                "entered_utr": user_utr,
                "verification_status": "pending",
                "status": "pending",
                "status_message": "Verification failed",
                **customer_patch,
            },
        )
        payment_repository.update(
            user_utr,
            {
                "verification_status": "rejected",
                "customer_name": customer_patch["customer_name"],
                "customer_email": customer_patch["customer_email"],
                "customer_mobile": customer_patch["customer_mobile"],
                "reference_note": customer_patch["reference_note"],
                "status_message": "Rejected due to amount mismatch.",
            },
        )
        publish_order_update(order)
        return jsonify({"error": "Verification failed"}), 422

    order = repository.update(
        order_id,
        {
            "entered_utr": user_utr,
            "verification_status": "success",
            "status": "success",
            "status_message": "Payment verified successfully",
            "payment_reference": user_utr,
            "verified_at": utc_now_iso(),
            "verified_payment_id": payment.get("payment_id"),
            **customer_patch,
        },
    )
    payment_repository.update(
        user_utr,
        {
            "verification_status": "verified",
            "order_id": order_id,
            "verified_order_id": order_id,
            "status_message": "Matched against checkout order.",
            "verified_at": utc_now_iso(),
            "customer_name": customer_patch["customer_name"],
            "customer_email": customer_patch["customer_email"],
            "customer_mobile": customer_patch["customer_mobile"],
            "reference_note": customer_patch["reference_note"],
        },
    )
    publish_order_update(order)
    return jsonify(
        {
            "message": "UTR verification processed",
            "matched": True,
        }
    )


@app.route("/api/orders/<order_id>/simulate", methods=["POST"])
@require_checkout_session
def simulate_verification(order_id: str):
    payload = request.get_json(silent=True) or {}
    status_value = (payload.get("status") or "success").lower()
    message = payload.get("message") or "Simulated verification event"
    order = get_checkout_order(order_id)
    if not order:
        return jsonify({"error": "Order not found"}), 404

    order = repository.update(
        order_id,
        {
            "verification_status": "failed" if status_value == "failure" else status_value,
            "status": "failed" if status_value == "failure" else status_value,
            "status_message": message,
            "payment_reference": payload.get("payment_reference") or make_txn_id(),
            "verified_at": utc_now_iso(),
            "verification_payload": {"source": "simulate", **payload},
        },
    )
    publish_order_update(order)
    return jsonify({"message": "Simulation published", "order": public_order(order)})


@socketio.on("connect")
def socket_connect(auth=None):
    order_id = None
    if auth and isinstance(auth, dict):
        order_id = auth.get("order_id")
    if not order_id:
        order_id = session.get("active_order_id")

    if not order_id or not session_authorized(order_id):
        return False


@socketio.on("subscribe_order")
def subscribe_order(data):
    order_id = (data or {}).get("order_id") or session.get("active_order_id")
    if not order_id or not session_authorized(order_id):
        emit("verification_error", {"message": "Unauthorized checkout session"})
        disconnect()
        return

    join_room(room_name(order_id))
    order = repository.get(order_id)
    if order:
        emit("verification_update", public_order(order))


@socketio.on("ping_order")
def ping_order(data):
    order_id = (data or {}).get("order_id") or session.get("active_order_id")
    if not order_id or not session_authorized(order_id):
        emit("verification_error", {"message": "Unauthorized checkout session"})
        disconnect()
        return

    order = repository.get(order_id)
    emit(
        "verification_update",
        public_order(order) if order else {"order_id": order_id, "verification_status": "unknown"},
    )


@app.errorhandler(403)
def forbidden(_):
    return jsonify({"error": "Forbidden"}), 403


@app.errorhandler(404)
def not_found(_):
    return jsonify({"error": "Not found"}), 404


if __name__ == "__main__":
    socketio.run(app, debug=True, host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
