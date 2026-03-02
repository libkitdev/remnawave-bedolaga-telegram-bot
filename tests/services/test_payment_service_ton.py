"""Тесты для TonPaymentMixin и TonPriceService."""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.database.crud import ton as ton_crud
from app.services.payment_service import PaymentService
from app.services.ton_price_service import TonPriceService


# ---------------------------------------------------------------------------
# Вспомогательные классы
# ---------------------------------------------------------------------------


class DummySession:
    def __init__(self) -> None:
        self.added_objects: list[Any] = []
        self._scalar_result: Any = None

    async def commit(self) -> None:
        return None

    async def refresh(self, obj: Any) -> None:
        return None

    def add(self, obj: Any) -> None:
        self.added_objects.append(obj)

    async def execute(self, stmt: Any) -> Any:
        return self

    def scalar_one_or_none(self) -> Any:
        return self._scalar_result


class DummyTonPayment:
    def __init__(
        self,
        *,
        payment_id: int = 1,
        user_id: int = 42,
        memo: str = 'ton_42_abcdef',
        amount_kopeks: int = 15000,
        amount_nano: int = 3_000_000_000,
        status: str = 'pending',
        transaction_id: int | None = None,
        ton_hash: str | None = None,
        expires_at: datetime | None = None,
        created_at: datetime | None = None,
    ) -> None:
        self.id = payment_id
        self.user_id = user_id
        self.memo = memo
        self.amount_kopeks = amount_kopeks
        self.amount_nano = amount_nano
        self.status = status
        self.transaction_id = transaction_id
        self.ton_hash = ton_hash
        self.expires_at = expires_at or datetime.now(UTC) + timedelta(hours=1)
        self.created_at = created_at or datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        self.metadata_json: dict = {}
        self.is_paid = status == 'paid'


def _make_service(ton_price_service: Any = None) -> PaymentService:
    service = PaymentService.__new__(PaymentService)  # type: ignore[call-arg]
    service.bot = None
    service.ton_price_service = ton_price_service
    service.heleket_service = None
    service.yookassa_service = None
    service.stars_service = None
    service.cryptobot_service = None
    service.mulenpay_service = None
    service.pal24_service = None
    service.wata_service = None
    service.platega_service = None
    service.cloudpayments_service = None
    service.nalogo_service = None
    return service


class StubTonPriceService:
    def __init__(self, nano: int | None = 3_000_000_000) -> None:
        self._nano = nano

    async def rub_to_nano(self, rub_amount: float) -> int | None:
        return self._nano


# ---------------------------------------------------------------------------
# Тесты TonPriceService
# ---------------------------------------------------------------------------


@pytest.mark.anyio('asyncio')
async def test_ton_price_service_caches_rate() -> None:
    service = TonPriceService()

    async def fake_fetch() -> float:
        return 500.0

    with patch.object(service, '_fetch_rate', side_effect=fake_fetch):
        rate1 = await service.get_rate_rub()
        rate2 = await service.get_rate_rub()

    assert rate1 == 500.0
    assert rate2 == 500.0  # второй вызов из кеша


@pytest.mark.anyio('asyncio')
async def test_ton_price_service_fallback_on_error() -> None:
    service = TonPriceService()
    service._cached_rate = 450.0
    service._cached_at = 0.0  # кеш устарел

    async def fake_fetch() -> None:
        return None

    with patch.object(service, '_fetch_rate', side_effect=fake_fetch):
        rate = await service.get_rate_rub()

    assert rate == 450.0  # вернул устаревший кеш


@pytest.mark.anyio('asyncio')
async def test_ton_price_service_rub_to_nano() -> None:
    service = TonPriceService()
    service._cached_rate = 500.0
    service._cached_at = 9_999_999_999.0

    result = await service.rub_to_nano(100.0)
    # 100 RUB / 500 RUB/TON = 0.2 TON = 200_000_000 нано
    assert result == 200_000_000


@pytest.mark.anyio('asyncio')
async def test_ton_price_service_rub_to_nano_returns_none_on_no_rate() -> None:
    service = TonPriceService()

    async def fake_fetch() -> None:
        return None

    with patch.object(service, '_fetch_rate', side_effect=fake_fetch):
        result = await service.rub_to_nano(100.0)

    assert result is None


# ---------------------------------------------------------------------------
# Тесты create_ton_payment
# ---------------------------------------------------------------------------


@pytest.mark.anyio('asyncio')
async def test_create_ton_payment_success(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_price = StubTonPriceService(nano=3_000_000_000)
    service = _make_service(stub_price)
    db = DummySession()
    created_payment = DummyTonPayment()

    captured: dict[str, Any] = {}

    async def fake_create(db: Any, **kwargs: Any) -> DummyTonPayment:
        captured.update(kwargs)
        return created_payment

    monkeypatch.setattr(ton_crud, 'create_ton_payment', fake_create, raising=False)
    monkeypatch.setattr('app.config.settings.TON_WALLET_ADDRESS', '0:wallet', raising=False)
    monkeypatch.setattr('app.config.settings.TON_INVOICE_TTL_MINUTES', 60, raising=False)

    result = await service.create_ton_payment(
        db=db,
        user_id=42,
        amount_kopeks=15000,
        description='Пополнение',
    )

    assert result is not None
    assert result['local_payment_id'] == created_payment.id
    assert result['memo'].startswith('ton_42_')
    assert result['amount_nano'] == 3_000_000_000
    assert result['amount_kopeks'] == 15000
    assert result['wallet_address'] == '0:wallet'
    assert captured['user_id'] == 42
    assert captured['amount_kopeks'] == 15000
    assert captured['amount_nano'] == 3_000_000_000


@pytest.mark.anyio('asyncio')
async def test_create_ton_payment_returns_none_without_price_service() -> None:
    service = _make_service(ton_price_service=None)
    db = DummySession()

    result = await service.create_ton_payment(db=db, user_id=1, amount_kopeks=10000, description='test')

    assert result is None


@pytest.mark.anyio('asyncio')
async def test_create_ton_payment_returns_none_when_rate_unavailable() -> None:
    service = _make_service(StubTonPriceService(nano=None))
    db = DummySession()

    result = await service.create_ton_payment(db=db, user_id=1, amount_kopeks=10000, description='test')

    assert result is None


@pytest.mark.anyio('asyncio')
async def test_create_ton_payment_returns_none_on_zero_amount() -> None:
    service = _make_service(StubTonPriceService())
    db = DummySession()

    result = await service.create_ton_payment(db=db, user_id=1, amount_kopeks=0, description='test')

    assert result is None


# ---------------------------------------------------------------------------
# Тесты process_ton_webhook
# ---------------------------------------------------------------------------


def _make_payload(
    *,
    memo: str = 'ton_42_abcdef',
    value: int = 3_000_000_000,
    success: bool = True,
    ton_hash: str = 'abc123',
) -> dict[str, Any]:
    return {
        'hash': ton_hash,
        'success': success,
        'transaction_data': {
            'in_msg': {
                'value': value,
                'decoded_body': {'text': memo},
            }
        },
    }


@pytest.mark.anyio('asyncio')
async def test_webhook_returns_true_on_non_success(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service()
    db = DummySession()

    payload = _make_payload(success=False)
    result = await service.process_ton_webhook(db, payload)

    assert result is True  # не ошибка, просто не наша задача


@pytest.mark.anyio('asyncio')
async def test_webhook_returns_true_when_memo_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service()
    db = DummySession()

    payload = {'success': True, 'transaction_data': {'in_msg': {'value': 1_000_000_000}}}
    result = await service.process_ton_webhook(db, payload)

    assert result is True


@pytest.mark.anyio('asyncio')
async def test_webhook_returns_true_when_memo_format_wrong(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service()
    db = DummySession()

    payload = _make_payload(memo='random_comment')
    result = await service.process_ton_webhook(db, payload)

    assert result is True


@pytest.mark.anyio('asyncio')
async def test_webhook_returns_true_when_payment_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service()
    db = DummySession()
    db._scalar_result = None  # платёж не найден

    result = await service.process_ton_webhook(db, _make_payload())

    assert result is True


@pytest.mark.anyio('asyncio')
async def test_webhook_returns_true_when_already_paid(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service()
    db = DummySession()
    db._scalar_result = DummyTonPayment(status='paid')

    result = await service.process_ton_webhook(db, _make_payload())

    assert result is True


@pytest.mark.anyio('asyncio')
async def test_webhook_returns_true_when_already_linked(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service()
    db = DummySession()
    db._scalar_result = DummyTonPayment(transaction_id=999)

    result = await service.process_ton_webhook(db, _make_payload())

    assert result is True


@pytest.mark.anyio('asyncio')
async def test_webhook_returns_true_when_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service()
    db = DummySession()
    expired_payment = DummyTonPayment(expires_at=datetime.now(UTC) - timedelta(hours=1))
    db._scalar_result = expired_payment

    update_called = False

    async def fake_update(db, payment, **kwargs: Any) -> DummyTonPayment:
        nonlocal update_called
        update_called = True
        assert kwargs.get('status') == 'expired'
        return payment

    monkeypatch.setattr(ton_crud, 'update_ton_payment', fake_update, raising=False)

    result = await service.process_ton_webhook(db, _make_payload())

    assert result is True
    assert update_called


@pytest.mark.anyio('asyncio')
async def test_webhook_returns_true_on_insufficient_amount(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service()
    db = DummySession()
    # expected 3 TON, получили только 1 TON (< 97%)
    db._scalar_result = DummyTonPayment(amount_nano=3_000_000_000)

    monkeypatch.setattr('app.config.settings.TON_MIN_AMOUNT_RATIO', 0.97, raising=False)

    result = await service.process_ton_webhook(db, _make_payload(value=1_000_000_000))

    assert result is True  # недоплата, но не ошибка сервера


@pytest.mark.anyio('asyncio')
async def test_webhook_idempotency_via_existing_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Проверка идемпотентности через get_transaction_by_external_id."""
    service = _make_service()
    db = DummySession()
    db._scalar_result = DummyTonPayment()

    existing_tx = SimpleNamespace(id=777)

    async def fake_get_tx(db, external_id, method):
        return existing_tx

    monkeypatch.setattr('app.config.settings.TON_MIN_AMOUNT_RATIO', 0.97, raising=False)

    import app.services.payment_service as ps_module

    monkeypatch.setattr(ps_module, 'get_transaction_by_external_id', fake_get_tx, raising=False)

    result = await service.process_ton_webhook(db, _make_payload())

    assert result is True  # уже обработано


@pytest.mark.anyio('asyncio')
async def test_webhook_success_calls_finalize(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service()

    payment = DummyTonPayment(amount_nano=3_000_000_000)

    # Первый execute — FOR UPDATE → платёж, второй — проверка hash → None (не дубль)
    call_count = 0

    class SmartSession:
        async def commit(self) -> None:
            return None

        async def refresh(self, obj: Any) -> None:
            return None

        def add(self, obj: Any) -> None:
            pass

        async def execute(self, stmt: Any) -> Any:
            nonlocal call_count
            call_count += 1
            return self

        def scalar_one_or_none(self) -> Any:
            if call_count == 1:
                return payment   # FOR UPDATE → наш платёж
            return None          # проверка hash → не дубль

    monkeypatch.setattr('app.config.settings.TON_MIN_AMOUNT_RATIO', 0.97, raising=False)

    finalize_called = False

    async def fake_finalize(self, db, payment, payload, *, ton_hash=None) -> bool:
        nonlocal finalize_called
        finalize_called = True
        return True

    import app.services.payment_service as ps_module

    monkeypatch.setattr(ps_module, 'get_transaction_by_external_id', AsyncMock(return_value=None), raising=False)
    monkeypatch.setattr(PaymentService, '_finalize_ton_payment', fake_finalize, raising=False)

    result = await service.process_ton_webhook(SmartSession(), _make_payload(value=3_000_000_000))

    assert result is True
    assert finalize_called


@pytest.mark.anyio('asyncio')
async def test_webhook_duplicate_hash_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Транзакция с уже известным ton_hash не должна зачисляться повторно."""
    service = _make_service()

    # Первый execute — FOR UPDATE (возвращает pending-платёж)
    # Второй execute — проверка hash (возвращает уже существующий платёж)
    call_count = 0

    class DualSession:
        async def commit(self) -> None:
            return None

        async def refresh(self, obj: Any) -> None:
            return None

        def add(self, obj: Any) -> None:
            pass

        async def execute(self, stmt: Any) -> Any:
            nonlocal call_count
            call_count += 1
            return self

        def scalar_one_or_none(self) -> Any:
            if call_count == 1:
                return DummyTonPayment()  # FOR UPDATE → pending-платёж
            return DummyTonPayment(status='paid')  # проверка hash → уже обработан

    import app.services.payment_service as ps_module

    monkeypatch.setattr('app.config.settings.TON_MIN_AMOUNT_RATIO', 0.97, raising=False)
    monkeypatch.setattr(ps_module, 'get_transaction_by_external_id', AsyncMock(return_value=None), raising=False)

    result = await service.process_ton_webhook(DualSession(), _make_payload())

    assert result is True
    assert call_count == 2  # оба запроса выполнены
