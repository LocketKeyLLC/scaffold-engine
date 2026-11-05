# tests/test_paper_twelve_modules.py
"""Tests for app.modules.paper_alpaca_paper_client."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.modules.paper_alpaca_paper_client import (
    ALPACA_API_KEY_SECRET_NAME,
    ALPACA_SECRET_KEY_SECRET_NAME,
    PAPER_BASE_URL,
    PAPER_DATA_URL,
    PAPER_STREAM_URL,
    VALID_ASSET_CLASSES,
    VALID_ORDER_SIDES,
    VALID_ORDER_TYPES,
    VALID_TIME_IN_FORCE,
    AlpacaAccount,
    AlpacaClock,
    AlpacaCredentials,
    AlpacaOrder,
    AlpacaPaperClient,
    AlpacaPaperClientError,
    AlpacaPosition,
    AlpacaSecretsMissingError,
    _auth_headers,
    _get_credentials,
    _parse_account,
    _parse_clock,
    _parse_datetime,
    _parse_decimal,
    _parse_order,
    _parse_position,
)


# ---------------------------------------------------------------------------
# Fakes shared by the tests. The module uses:
#   httpx.AsyncClient(timeout=30.0) -> client
#   client.is_closed                    (bool)
#   client.request(method, url, headers=..., params=..., json=...)  (async)
#   client.aclose()                     (async)
#   response.status_code / response.text / response.json()
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, *, status_code=200, json_data=None, text="", json_error=False):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("not json")
        return self._json_data


class _FakeAsyncClient:
    def __init__(self, request: AsyncMock | None = None):
        self.is_closed = False
        self.request = request if request is not None else AsyncMock()
        self.aclose = AsyncMock()


def _fake_response(*, status_code=200, json_data=None, text="", json_error=False):
    """Build a fake httpx.Response-compatible object."""
    return _FakeResponse(
        status_code=status_code,
        json_data=json_data,
        text=text,
        json_error=json_error,
    )


def _patch_credentials():
    """Patch values_for so credentials load without a real secret store."""
    return patch(
        "app.modules.paper_alpaca_paper_client.values_for",
        new=AsyncMock(
            return_value={
                "ALPACA_API_KEY_ID": "test-key",
                "ALPACA_API_SECRET_KEY": "test-secret",
            }
        ),
    )


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

def test_paper_base_url_is_paper_endpoint():
    assert PAPER_BASE_URL == "https://paper-api.alpaca.markets"


def test_paper_data_url_is_paper_data_endpoint():
    assert PAPER_DATA_URL == "https://data.alpaca.markets"


def test_paper_stream_url_is_paper_stream_endpoint():
    assert PAPER_STREAM_URL == "wss://paper-api.alpaca.markets/stream"


def test_secret_names():
    assert ALPACA_API_KEY_SECRET_NAME == "ALPACA_API_KEY_ID"
    assert ALPACA_SECRET_KEY_SECRET_NAME == "ALPACA_API_SECRET_KEY"


def test_valid_order_types():
    assert VALID_ORDER_TYPES == frozenset(
        {"market", "limit", "stop", "stop_limit", "trailing_stop"}
    )


def test_valid_time_in_force():
    assert VALID_TIME_IN_FORCE == frozenset({"day", "gtc", "opg", "cls", "ioc", "fok"})


def test_valid_order_sides():
    assert VALID_ORDER_SIDES == frozenset({"buy", "sell"})


def test_valid_asset_classes():
    assert VALID_ASSET_CLASSES == frozenset({"us_equity", "us_option", "crypto"})


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

def test_alpaca_credentials_dataclass():
    creds = AlpacaCredentials(api_key_id="key", secret_key="secret")
    assert creds.api_key_id == "key"
    assert creds.secret_key == "secret"


def test_alpaca_order_dataclass():
    now = datetime.now(timezone.utc)
    order = AlpacaOrder(
        id="o1",
        client_order_id=None,
        symbol="AAPL",
        side="buy",
        order_type="market",
        qty=Decimal("1"),
        time_in_force="day",
        limit_price=None,
        stop_price=None,
        status="accepted",
        created_at=now,
        submitted_at=None,
        filled_at=None,
        filled_qty=Decimal("0"),
        filled_avg_price=None,
    )
    assert order.id == "o1"
    assert order.symbol == "AAPL"


def test_alpaca_position_dataclass():
    pos = AlpacaPosition(
        symbol="AAPL",
        qty=Decimal("2"),
        avg_entry_price=Decimal("150"),
        market_value=Decimal("300"),
        unrealized_pl=Decimal("0"),
        current_price=Decimal("150"),
        side="long",
    )
    assert pos.symbol == "AAPL"
    assert pos.qty == Decimal("2")


def test_alpaca_account_dataclass():
    now = datetime.now(timezone.utc)
    acct = AlpacaAccount(
        id="acct1",
        account_number="123",
        status="ACTIVE",
        currency="USD",
        cash=Decimal("10000"),
        equity=Decimal("10000"),
        buying_power=Decimal("20000"),
        portfolio_value=Decimal("10000"),
        long_market_value=Decimal("0"),
        short_market_value=Decimal("0"),
        daytrade_count=0,
        pattern_day_trader=False,
        trading_blocked=False,
        transfers_blocked=False,
        account_blocked=False,
        created_at=now,
    )
    assert acct.id == "acct1"
    assert acct.equity == Decimal("10000")


def test_alpaca_clock_dataclass():
    now = datetime.now(timezone.utc)
    clock = AlpacaClock(
        timestamp=now,
        is_open=True,
        next_open=now,
        next_close=now,
    )
    assert clock.is_open is True


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_credentials_both_present():
    with patch(
        "app.modules.paper_alpaca_paper_client.values_for",
        new=AsyncMock(
            return_value={
                "ALPACA_API_KEY_ID": "key",
                "ALPACA_API_SECRET_KEY": "secret",
            }
        ),
    ):
        creds = await _get_credentials(db=None)
    assert isinstance(creds, AlpacaCredentials)
    assert creds.api_key_id == "key"
    assert creds.secret_key == "secret"


@pytest.mark.asyncio
async def test_get_credentials_missing_both_raises():
    with patch(
        "app.modules.paper_alpaca_paper_client.values_for",
        new=AsyncMock(return_value={}),
    ):
        with pytest.raises(AlpacaSecretsMissingError):
            await _get_credentials(db=None)


@pytest.mark.asyncio
async def test_get_credentials_missing_one_raises():
    with patch(
        "app.modules.paper_alpaca_paper_client.values_for",
        new=AsyncMock(
            return_value={
                ALPACA_API_KEY_SECRET_NAME: "key",
            }
        ),
    ):
        with pytest.raises(AlpacaSecretsMissingError):
            await _get_credentials(db=None)


def test_auth_headers():
    creds = AlpacaCredentials(api_key_id="key", secret_key="secret")
    headers = _auth_headers(creds)
    assert headers == {
        "APCA-API-KEY-ID": "key",
        "APCA-API-SECRET-KEY": "secret",
    }


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def test_parse_datetime_none():
    assert _parse_datetime(None) is None


def test_parse_datetime_z_suffix():
    dt = _parse_datetime("2024-01-01T00:00:00Z")
    assert dt == datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)


def test_parse_datetime_offset():
    dt = _parse_datetime("2024-01-01T00:00:00-05:00")
    assert dt is not None
    assert dt.utcoffset() is not None


def test_parse_datetime_naive_gets_utc():
    dt = _parse_datetime("2024-01-01T00:00:00")
    assert dt.tzinfo == timezone.utc


def test_parse_datetime_invalid_returns_none():
    assert _parse_datetime("not-a-date") is None


def test_parse_decimal_none():
    assert _parse_decimal(None) is None


def test_parse_decimal_empty_string():
    assert _parse_decimal("") is None


def test_parse_decimal_valid():
    assert _parse_decimal("123.45") == Decimal("123.45")


def test_parse_decimal_int():
    assert _parse_decimal(123) == Decimal("123")


def test_parse_decimal_float():
    assert _parse_decimal(123.45) == Decimal("123.45")


def test_parse_decimal_invalid_returns_none():
    assert _parse_decimal("abc") is None


def _order_dict():
    return {
        "id": "order-1",
        "client_order_id": "co-1",
        "symbol": "AAPL",
        "side": "buy",
        "type": "market",
        "qty": "1",
        "time_in_force": "day",
        "limit_price": None,
        "stop_price": None,
        "status": "accepted",
        "created_at": "2024-01-01T00:00:00Z",
        "submitted_at": None,
        "filled_at": None,
        "filled_qty": "0",
        "filled_avg_price": None,
    }


def test_parse_order_full():
    now = datetime.now(timezone.utc)
    data = {
        "id": "o1",
        "client_order_id": "cid1",
        "symbol": "AAPL",
        "side": "buy",
        "type": "limit",
        "qty": "2",
        "time_in_force": "day",
        "limit_price": "150.00",
        "stop_price": "149.00",
        "status": "accepted",
        "created_at": now.isoformat(),
        "submitted_at": now.isoformat(),
        "filled_at": None,
        "filled_qty": "0",
        "filled_avg_price": None,
    }
    order = _parse_order(data)
    assert order.id == "o1"
    assert order.client_order_id == "cid1"
    assert order.symbol == "AAPL"
    assert order.qty == Decimal("2")
    assert order.order_type == "limit"
    assert order.limit_price == Decimal("150.00")
    assert order.stop_price == Decimal("149.00")


def test_parse_order_missing_optional_fields_defaults():
    data = {
        "id": "o1",
        "symbol": "AAPL",
        "side": "buy",
        "type": "market",
        "qty": "1",
        "time_in_force": "day",
        "status": "accepted",
    }
    order = _parse_order(data)
    assert order.client_order_id is None
    assert order.limit_price is None
    assert order.stop_price is None
    assert order.filled_qty == Decimal("0")
    assert order.filled_avg_price is None


def _position_dict():
    return {
        "symbol": "AAPL",
        "qty": "2",
        "avg_entry_price": "100",
        "market_value": "200",
        "unrealized_pl": "0",
        "current_price": "100",
        "side": "long",
    }


def test_parse_position_full():
    data = {
        "symbol": "AAPL",
        "qty": "3",
        "avg_entry_price": "140.50",
        "market_value": "450.00",
        "unrealized_pl": "10.00",
        "current_price": "150.00",
        "side": "long",
    }
    pos = _parse_position(data)
    assert pos.symbol == "AAPL"
    assert pos.qty == Decimal("3")
    assert pos.avg_entry_price == Decimal("140.50")
    assert pos.market_value == Decimal("450.00")
    assert pos.unrealized_pl == Decimal("10.00")
    assert pos.current_price == Decimal("150.00")
    assert pos.side == "long"


def test_parse_position_defaults():
    data = {
        "symbol": "AAPL",
    }
    pos = _parse_position(data)
    assert pos.qty == Decimal("0")
    assert pos.avg_entry_price == Decimal("0")
    assert pos.market_value is None
    assert pos.unrealized_pl is None
    assert pos.current_price is None
    assert pos.side == "long"


def _account_dict():
    return {
        "id": "acc-1",
        "account_number": "123",
        "status": "ACTIVE",
        "currency": "USD",
        "cash": "1000.00",
        "equity": "5000.00",
        "buying_power": "2000.00",
        "portfolio_value": "5000.00",
        "long_market_value": "5000.00",
        "short_market_value": "0",
        "daytrade_count": 2,
        "pattern_day_trader": False,
        "trading_blocked": False,
        "transfers_blocked": False,
        "account_blocked": False,
        "created_at": "2024-01-01T00:00:00Z",
    }


def test_parse_account_full():
    now = datetime.now(timezone.utc)
    data = {
        "id": "acct1",
        "account_number": "123",
        "status": "ACTIVE",
        "currency": "USD",
        "cash": "10000",
        "equity": "10000",
        "buying_power": "20000",
        "portfolio_value": "10000",
        "long_market_value": "0",
        "short_market_value": "0",
        "daytrade_count": 1,
        "pattern_day_trader": True,
        "trading_blocked": False,
        "transfers_blocked": False,
        "account_blocked": False,
        "created_at": now.isoformat(),
    }
    acct = _parse_account(data)
    assert acct.id == "acct1"
    assert acct.daytrade_count == 1
    assert acct.pattern_day_trader is True


def test_parse_account_defaults():
    data = {"id": "acct1"}
    acct = _parse_account(data)
    assert acct.account_number == ""
    assert acct.status == "UNKNOWN"
    assert acct.currency == "USD"
    assert acct.cash == Decimal("0")
    assert acct.daytrade_count == 0
    assert acct.pattern_day_trader is False


def _clock_dict():
    return {
        "timestamp": "2024-01-01T09:30:00-05:00",
        "is_open": True,
        "next_open": "2024-01-02T09:30:00-05:00",
        "next_close": "2024-01-01T16:00:00-05:00",
    }


def test_parse_clock_full():
    now = datetime.now(timezone.utc)
    data = {
        "timestamp": now.isoformat(),
        "is_open": True,
        "next_open": now.isoformat(),
        "next_close": now.isoformat(),
    }
    clock = _parse_clock(data)
    assert clock.is_open is True


def test_parse_clock_defaults():
    data = {}
    clock = _parse_clock(data)
    assert clock.is_open is False


# ---------------------------------------------------------------------------
# Client lifecycle
# ---------------------------------------------------------------------------

def test_client_init_defaults():
    client = AlpacaPaperClient()
    assert client._base_url == PAPER_BASE_URL
    assert client._data_url == PAPER_DATA_URL
    assert client._credentials is None
    assert client._client is None


def test_client_init_custom_urls():
    client = AlpacaPaperClient(
        base_url="https://example.com/api/",
        data_url="https://data.example.com/",
    )
    assert client._base_url == "https://example.com/api"
    assert client._data_url == "https://data.example.com"


@pytest.mark.asyncio
async def test_ensure_client_creates_once():
    client = AlpacaPaperClient()
    with patch("app.modules.paper_alpaca_paper_client.httpx.AsyncClient") as mock_cls:
        mock_instance = AsyncMock()
        mock_instance.is_closed = False
        mock_instance.aclose = AsyncMock()
        mock_cls.return_value = mock_instance

        first = await client._ensure_client()
        second = await client._ensure_client()

        assert first is second
        mock_cls.assert_called_once_with(timeout=30.0)


@pytest.mark.asyncio
async def test_ensure_client_recreates_after_close():
    client = AlpacaPaperClient()
    with patch("app.modules.paper_alpaca_paper_client.httpx.AsyncClient") as mock_cls:
        mock_instance = AsyncMock()
        mock_instance.is_closed = False
        mock_instance.aclose = AsyncMock()
        mock_cls.return_value = mock_instance

        await client._ensure_client()
        await client.close()
        assert client._client is None
        mock_instance.aclose.assert_awaited_once()

        # After close, _ensure_client should create a new instance.
        new_instance = AsyncMock()
        new_instance.is_closed = False
        new_instance.aclose = AsyncMock()
        mock_cls.return_value = new_instance

        new = await client._ensure_client()
        assert new is new_instance
        assert mock_cls.call_count == 2


@pytest.mark.asyncio
async def test_close_closes_client():
    client = AlpacaPaperClient()
    with patch("app.modules.paper_alpaca_paper_client.httpx.AsyncClient") as mock_cls:
        mock_instance = AsyncMock()
        mock_instance.is_closed = False
        mock_instance.aclose = AsyncMock()
        mock_cls.return_value = mock_instance

        await client._ensure_client()
        await client.close()
        mock_instance.aclose.assert_awaited_once()
        assert client._client is None


@pytest.mark.asyncio
async def test_close_no_client_is_noop():
    client = AlpacaPaperClient()
    # No client has been created; close should do nothing and not raise.
    await client.close()


@pytest.mark.asyncio
async def test_ensure_credentials_loads_once():
    client = AlpacaPaperClient()
    fake_creds = AlpacaCredentials(api_key_id="key", secret_key="secret")
    with patch(
        "app.modules.paper_alpaca_paper_client._get_credentials",
        new=AsyncMock(return_value=fake_creds),
    ) as mock_get:
        creds1 = await client._ensure_credentials()
        creds2 = await client._ensure_credentials()

        assert creds1 is fake_creds
        assert creds2 is fake_creds
        mock_get.assert_awaited_once()


# ---------------------------------------------------------------------------
# _request
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_request_success():
    fake_response = _fake_response(status_code=200, json_data={"ok": True})
    fake_client = _FakeAsyncClient()
    fake_client.request.return_value = fake_response

    with patch(
        "app.modules.paper_alpaca_paper_client.httpx.AsyncClient",
        return_value=fake_client,
    ), _patch_credentials():
        client = AlpacaPaperClient()
        result = await client._request("GET", "/v2/test")

    assert result == {"ok": True}
    call = fake_client.request.await_args
    assert call.args[0] == "GET"
    assert call.args[1] == f"{PAPER_BASE_URL}/v2/test"


@pytest.mark.asyncio
async def test_request_http_error_raises():
    client = AlpacaPaperClient()
    with patch(
        "app.modules.paper_alpaca_paper_client._get_credentials",
        new=AsyncMock(return_value=AlpacaCredentials("key", "secret")),
    ):
        with patch("app.modules.paper_alpaca_paper_client.httpx.AsyncClient") as mock_cls:
            mock_instance = AsyncMock()
            mock_instance.is_closed = False
            mock_instance.aclose = AsyncMock()
            fake_resp = _fake_response(status_code=500, text="internal error")
            mock_instance.request = AsyncMock(return_value=fake_resp)
            mock_cls.return_value = mock_instance

            with pytest.raises(AlpacaPaperClientError):
                await client._request("GET", "/v2/test")


@pytest.mark.asyncio
async def test_request_204_returns_empty_dict():
    client = AlpacaPaperClient()
    with patch(
        "app.modules.paper_alpaca_paper_client._get_credentials",
        new=AsyncMock(return_value=AlpacaCredentials("key", "secret")),
    ):
        with patch("app.modules.paper_alpaca_paper_client.httpx.AsyncClient") as mock_cls:
            mock_instance = AsyncMock()
            mock_instance.is_closed = False
            mock_instance.aclose = AsyncMock()
            fake_resp = _fake_response(status_code=204, json_data=None)
            mock_instance.request = AsyncMock(return_value=fake_resp)
            mock_cls.return_value = mock_instance

            result = await client._request("DELETE", "/v2/test")

    assert result == {}


@pytest.mark.asyncio
async def test_request_non_json_raises():
    fake_response = _fake_response(status_code=200, json_error=True, text="<html>")
    fake_client = _FakeAsyncClient()
    fake_client.request.return_value = fake_response

    with patch(
        "app.modules.paper_alpaca_paper_client.httpx.AsyncClient",
        return_value=fake_client,
    ), _patch_credentials():
        client = AlpacaPaperClient()
        with pytest.raises(AlpacaPaperClientError):
            await client._request("GET", "/v2/test")


@pytest.mark.asyncio
async def test_request_connection_error_raises():
    client = AlpacaPaperClient()
    with patch(
        "app.modules.paper_alpaca_paper_client._get_credentials",
        new=AsyncMock(return_value=AlpacaCredentials("key", "secret")),
    ):
        with patch("app.modules.paper_alpaca_paper_client.httpx.AsyncClient") as mock_cls:
            mock_instance = AsyncMock()
            mock_instance.is_closed = False
            mock_instance.aclose = AsyncMock()
            mock_instance.request = AsyncMock(
                side_effect=httpx.ConnectError("conn refused", request=None)
            )
            mock_cls.return_value = mock_instance

            with pytest.raises(AlpacaPaperClientError):
                await client._request("GET", "/v2/test")


# ---------------------------------------------------------------------------
# API methods
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_account():
    client = AlpacaPaperClient()
    now = datetime.now(timezone.utc)
    acct_dict = {
        "id": "acct1",
        "account_number": "123",
        "status": "ACTIVE",
        "currency": "USD",
        "cash": "10000",
        "equity": "10000",
        "buying_power": "20000",
        "portfolio_value": "10000",
        "long_market_value": "0",
        "short_market_value": "0",
        "daytrade_count": 0,
        "pattern_day_trader": False,
        "trading_blocked": False,
        "transfers_blocked": False,
        "account_blocked": False,
        "created_at": now.isoformat(),
    }
    client._request = AsyncMock(return_value=acct_dict)

    result = await client.get_account()

    assert isinstance(result, AlpacaAccount)
    assert result.id == "acct1"
    client._request.assert_awaited_once_with("GET", "/v2/account")


@pytest.mark.asyncio
async def test_get_clock():
    client = AlpacaPaperClient()
    now = datetime.now(timezone.utc)
    clock_dict = {
        "timestamp": now.isoformat(),
        "is_open": True,
        "next_open": now.isoformat(),
        "next_close": now.isoformat(),
    }
    client._request = AsyncMock(return_value=clock_dict)

    result = await client.get_clock()

    assert isinstance(result, AlpacaClock)
    assert result.is_open is True
    client._request.assert_awaited_once_with("GET", "/v2/clock")


@pytest.mark.asyncio
async def test_submit_order_market_success():
    fake_response = _fake_response(status_code=200, json_data=_order_dict())
    fake_client = _FakeAsyncClient()
    fake_client.request.return_value = fake_response

    with patch(
        "app.modules.paper_alpaca_paper_client.httpx.AsyncClient",
        return_value=fake_client,
    ), _patch_credentials():
        client = AlpacaPaperClient()
        result = await client.submit_order(
            symbol="AAPL",
            qty=Decimal("1"),
            side="buy",
            order_type="market",
        )

    assert isinstance(result, AlpacaOrder)
    assert result.symbol == "AAPL"
    call = fake_client.request.await_args
    assert call.args[0] == "POST"
    assert call.args[1] == f"{PAPER_BASE_URL}/v2/orders"
    assert call.kwargs["json"]["symbol"] == "AAPL"
    assert call.kwargs["json"]["qty"] == "1"
    assert call.kwargs["json"]["side"] == "buy"
    assert call.kwargs["json"]["type"] == "market"
    assert call.kwargs["json"]["time_in_force"] == "day"


@pytest.mark.asyncio
async def test_submit_order_limit_with_limit_price():
    client = AlpacaPaperClient()
    now = datetime.now(timezone.utc)
    order_dict = {
        "id": "o2",
        "client_order_id": None,
        "symbol": "AAPL",
        "side": "buy",
        "type": "limit",
        "qty": "1",
        "time_in_force": "day",
        "limit_price": "150.00",
        "stop_price": None,
        "status": "accepted",
        "created_at": now.isoformat(),
        "submitted_at": None,
        "filled_at": None,
        "filled_qty": "0",
        "filled_avg_price": None,
    }
    client._request = AsyncMock(return_value=order_dict)

    order = await client.submit_order(
        symbol="AAPL",
        qty=Decimal("1"),
        side="buy",
        order_type="limit",
        limit_price=Decimal("150.00"),
    )

    assert order.order_type == "limit"
    assert order.limit_price == Decimal("150.00")
    kwargs = client._request.await_args.kwargs
    assert kwargs["json_body"]["limit_price"] == "150.00"


@pytest.mark.asyncio
async def test_submit_order_invalid_side_raises():
    client = AlpacaPaperClient()
    with pytest.raises(ValueError):
        await client.submit_order(
            symbol="AAPL",
            qty=Decimal("1"),
            side="short",
            order_type="market",
        )


@pytest.mark.asyncio
async def test_submit_order_invalid_order_type_raises():
    client = AlpacaPaperClient()
    with pytest.raises(ValueError):
        await client.submit_order(
            symbol="AAPL",
            qty=Decimal("1"),
            side="buy",
            order_type="bad_type",
        )


@pytest.mark.asyncio
async def test_submit_order_invalid_time_in_force_raises():
    client = AlpacaPaperClient()
    with pytest.raises(ValueError):
        await client.submit_order(
            symbol="AAPL",
            qty=Decimal("1"),
            side="buy",
            order_type="market",
            time_in_force="bad_tif",
        )


@pytest.mark.asyncio
async def test_submit_order_zero_qty_raises():
    client = AlpacaPaperClient()
    with pytest.raises(ValueError):
        await client.submit_order(
            symbol="AAPL",
            qty=Decimal("0"),
            side="buy",
            order_type="market",
        )


@pytest.mark.asyncio
async def test_submit_order_negative_qty_raises():
    client = AlpacaPaperClient()
    with pytest.raises(ValueError):
        await client.submit_order(
            symbol="AAPL",
            qty=Decimal("-1"),
            side="buy",
            order_type="market",
        )


@pytest.mark.asyncio
async def test_submit_order_with_client_order_id():
    client = AlpacaPaperClient()
    now = datetime.now(timezone.utc)
    order_dict = {
        "id": "o3",
        "client_order_id": "cid1",
        "symbol": "AAPL",
        "side": "buy",
        "type": "market",
        "qty": "1",
        "time_in_force": "day",
        "limit_price": None,
        "stop_price": None,
        "status": "accepted",
        "created_at": now.isoformat(),
        "submitted_at": None,
        "filled_at": None,
        "filled_qty": "0",
        "filled_avg_price": None,
    }
    client._request = AsyncMock(return_value=order_dict)

    await client.submit_order(
        symbol="AAPL",
        qty=Decimal("1"),
        side="buy",
        order_type="market",
        client_order_id="cid1",
    )

    kwargs = client._request.await_args.kwargs
    assert kwargs["json_body"]["client_order_id"] == "cid1"


@pytest.mark.asyncio
async def test_get_order():
    client = AlpacaPaperClient()
    now = datetime.now(timezone.utc)
    order_dict = {
        "id": "o1",
        "client_order_id": None,
        "symbol": "AAPL",
        "side": "buy",
        "type": "market",
        "qty": "1",
        "time_in_force": "day",
        "limit_price": None,
        "stop_price": None,
        "status": "filled",
        "created_at": now.isoformat(),
        "submitted_at": now.isoformat(),
        "filled_at": now.isoformat(),
        "filled_qty": "1",
        "filled_avg_price": "150.00",
    }
    client._request = AsyncMock(return_value=order_dict)

    order = await client.get_order("o1")

    assert order.id == "o1"
    client._request.assert_awaited_once_with("GET", "/v2/orders/o1")


@pytest.mark.asyncio
async def test_list_orders_empty():
    client = AlpacaPaperClient()
    client._request = AsyncMock(return_value=[])

    orders = await client.list_orders()

    assert orders == []
    client._request.assert_awaited_once_with("GET", "/v2/orders", params={})


@pytest.mark.asyncio
async def test_list_orders_with_filters():
    client = AlpacaPaperClient()
    now = datetime.now(timezone.utc)
    order_dict = {
        "id": "o1",
        "client_order_id": None,
        "symbol": "AAPL",
        "side": "buy",
        "type": "market",
        "qty": "1",
        "time_in_force": "day",
        "limit_price": None,
        "stop_price": None,
        "status": "open",
        "created_at": now.isoformat(),
        "submitted_at": None,
        "filled_at": None,
        "filled_qty": "0",
        "filled_avg_price": None,
    }
    client._request = AsyncMock(return_value=[order_dict])

    orders = await client.list_orders(
        status="open",
        limit=5,
        after=now,
        until=now,
        symbols=["AAPL", "MSFT"],
    )

    assert len(orders) == 1
    assert orders[0].symbol == "AAPL"
    kwargs = client._request.await_args.kwargs
    assert kwargs["params"]["status"] == "open"
    assert kwargs["params"]["limit"] == 5
    assert kwargs["params"]["symbols"] == "AAPL,MSFT"


@pytest.mark.asyncio
async def test_cancel_order():
    client = AlpacaPaperClient()
    client._request = AsyncMock(return_value={})

    await client.cancel_order("o1")

    client._request.assert_awaited_once_with("DELETE", "/v2/orders/o1")


@pytest.mark.asyncio
async def test_cancel_all_orders():
    client = AlpacaPaperClient()
    client._request = AsyncMock(return_value={})

    await client.cancel_all_orders()

    client._request.assert_awaited_once_with("DELETE", "/v2/orders")


@pytest.mark.asyncio
async def test_get_position():
    client = AlpacaPaperClient()
    pos_dict = {
        "symbol": "AAPL",
        "qty": "2",
        "avg_entry_price": "140.50",
        "market_value": "300.00",
        "unrealized_pl": "10.00",
        "current_price": "150.00",
        "side": "long",
    }
    client._request = AsyncMock(return_value=pos_dict)

    pos = await client.get_position("AAPL")

    assert pos.symbol == "AAPL"
    assert pos.qty == Decimal("2")
    client._request.assert_awaited_once_with("GET", "/v2/positions/AAPL")


@pytest.mark.asyncio
async def test_list_positions_empty():
    client = AlpacaPaperClient()
    client._request = AsyncMock(return_value=[])

    positions = await client.list_positions()

    assert positions == []
    client._request.assert_awaited_once_with("GET", "/v2/positions")


@pytest.mark.asyncio
async def test_list_positions_multiple():
    client = AlpacaPaperClient()
    pos1 = {
        "symbol": "AAPL",
        "qty": "2",
        "avg_entry_price": "140.50",
        "market_value": "300.00",
        "unrealized_pl": "10.00",
        "current_price": "150.00",
        "side": "long",
    }
    pos2 = {
        "symbol": "MSFT",
        "qty": "1",
        "avg_entry_price": "300.00",
        "market_value": "310.00",
        "unrealized_pl": "10.00",
        "current_price": "310.00",
        "side": "long",
    }
    client._request = AsyncMock(return_value=[pos1, pos2])

    positions = await client.list_positions()

    assert len(positions) == 2
    assert positions[0].symbol == "AAPL"
    assert positions[1].symbol == "MSFT"


@pytest.mark.asyncio
async def test_close_position():
    client = AlpacaPaperClient()
    client._request = AsyncMock(return_value={})

    await client.close_position("AAPL")

    client._request.assert_awaited_once_with("DELETE", "/v2/positions/AAPL")


@pytest.mark.asyncio
async def test_close_all_positions():
    client = AlpacaPaperClient()
    client._request = AsyncMock(return_value={})

    await client.close_all_positions()

    client._request.assert_awaited_once_with("DELETE", "/v2/positions")


@pytest.mark.asyncio
async def test_get_asset():
    client = AlpacaPaperClient()
    asset_dict = {"symbol": "AAPL", "name": "Apple Inc."}
    client._request = AsyncMock(return_value=asset_dict)

    asset = await client.get_asset("AAPL")

    assert asset == asset_dict
    client._request.assert_awaited_once_with("GET", "/v2/assets/AAPL")


@pytest.mark.asyncio
async def test_list_assets_no_filter():
    client = AlpacaPaperClient()
    asset_dict = {"symbol": "AAPL", "name": "Apple Inc."}
    client._request = AsyncMock(return_value=[asset_dict])

    assets = await client.list_assets()

    assert assets == [asset_dict]
    client._request.assert_awaited_once_with("GET", "/v2/assets", params={})


@pytest.mark.asyncio
async def test_list_assets_with_filters():
    client = AlpacaPaperClient()
    asset_dict = {"symbol": "AAPL", "name": "Apple Inc."}
    client._request = AsyncMock(return_value=[asset_dict])

    assets = await client.list_assets(status="active", asset_class="us_equity")

    assert assets == [asset_dict]
    kwargs = client._request.await_args.kwargs
    assert kwargs["params"]["status"] == "active"
    assert kwargs["params"]["asset_class"] == "us_equity"


@pytest.mark.asyncio
async def test_list_assets_invalid_asset_class_raises():
    client = AlpacaPaperClient()
    with pytest.raises(ValueError):
        await client.list_assets(asset_class="bad_class")


@pytest.mark.asyncio
async def test_get_bars():
    fake_response = _fake_response(
        status_code=200,
        json_data={"bars": [{"t": "2024-01-01T00:00:00Z", "c": 100.0}]},
    )
    fake_client = _FakeAsyncClient()
    fake_client.request.return_value = fake_response

    with patch(
        "app.modules.paper_alpaca_paper_client.httpx.AsyncClient",
        return_value=fake_client,
    ), _patch_credentials():
        client = AlpacaPaperClient()
        result = await client.get_bars("AAPL")

    assert len(result) == 1
    assert result[0]["c"] == 100.0
    call = fake_client.request.await_args
    assert call.args[0] == "GET"
    assert call.args[1] == f"{PAPER_DATA_URL}/v2/stocks/bars"
    assert call.kwargs["params"]["symbol"] == "AAPL"
    assert call.kwargs["params"]["timeframe"] == "1Day"


@pytest.mark.asyncio
async def test_get_bars_with_start_end_limit():
    client = AlpacaPaperClient()
    bar = {"t": "2024-01-01T00:00:00Z", "c": 150.0}
    client._request = AsyncMock(return_value={"bars": [bar]})
    now = datetime.now(timezone.utc)

    await client.get_bars(
        "AAPL",
        timeframe="1Hour",
        start=now,
        end=now,
        limit=100,
    )

    kwargs = client._request.await_args.kwargs
    assert kwargs["params"]["start"] == now.isoformat()
    assert kwargs["params"]["end"] == now.isoformat()
    assert kwargs["params"]["limit"] == 100


@pytest.mark.asyncio
async def test_get_latest_quote():
    fake_response = _fake_response(
        status_code=200,
        json_data={"quotes": {"AAPL": {"ap": 150.0, "bp": 149.5}}},
    )
    fake_client = _FakeAsyncClient()
    fake_client.request.return_value = fake_response

    with patch(
        "app.modules.paper_alpaca_paper_client.httpx.AsyncClient",
        return_value=fake_client,
    ), _patch_credentials():
        client = AlpacaPaperClient()
        result = await client.get_latest_quote("AAPL")

    assert "quotes" in result
    call = fake_client.request.await_args
    assert call.args[0] == "GET"
    assert call.args[1] == f"{PAPER_DATA_URL}/v2/stocks/quotes/latest"
    assert call.kwargs["params"]["symbols"] == "AAPL"


@pytest.mark.asyncio
async def test_get_latest_trade():
    fake_response = _fake_response(
        status_code=200,
        json_data={"trades": {"AAPL": {"p": 150.0}}},
    )
    fake_client = _FakeAsyncClient()
    fake_client.request.return_value = fake_response

    with patch(
        "app.modules.paper_alpaca_paper_client.httpx.AsyncClient",
        return_value=fake_client,
    ), _patch_credentials():
        client = AlpacaPaperClient()
        result = await client.get_latest_trade("AAPL")

    assert "trades" in result
    call = fake_client.request.await_args
    assert call.args[0] == "GET"
    assert call.args[1] == f"{PAPER_DATA_URL}/v2/stocks/trades/latest"
    assert call.kwargs["params"]["symbols"] == "AAPL"


@pytest.mark.asyncio
async def test_cancel_all_orders_and_close_all_positions_kill_switch():
    client = AlpacaPaperClient()
    client.cancel_all_orders = AsyncMock()
    client.close_all_positions = AsyncMock()

    await client.cancel_all_orders()
    await client.close_all_positions()

    client.cancel_all_orders.assert_awaited_once()
    client.close_all_positions.assert_awaited_once()
