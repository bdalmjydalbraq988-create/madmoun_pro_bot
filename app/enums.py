from enum import StrEnum


class FulfillmentMode(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


class OrderStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    PROVIDER_PENDING = "provider_pending"
    REVIEW_REQUIRED = "review_required"
    COMPLETED = "completed"
    FAILED = "failed"
    REFUNDED = "refunded"
    CANCELED = "canceled"


class LedgerKind(StrEnum):
    DEPOSIT = "deposit"
    PURCHASE = "purchase"
    REFUND = "refund"
    ADMIN_CREDIT = "admin_credit"
    ADMIN_DEBIT = "admin_debit"


class PaymentKind(StrEnum):
    BINANCE_PAY = "binance_pay"
    MANUAL = "manual"


class PaymentStatus(StrEnum):
    PENDING = "pending"
    REVIEW_REQUIRED = "review_required"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ProviderResultStatus(StrEnum):
    COMPLETED = "completed"
    PENDING = "pending"
