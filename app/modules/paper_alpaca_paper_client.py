# app/modules/paper_alpaca_paper_client.py
"""Alpaca paper trading sandbox client for the scaffold engine.

Provides an async client for the Alpaca paper trading API. All calls go to
the paper sandbox (https://paper-api.alpaca.markets) — never the live
endpoint. Secrets (API key id and secret key) are held by the engine and
read with `runner_secrets.values_for`; they are never logged or exposed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

import httpx

from app.config import settings
from app.database import async_session
from app.modules.runner_secrets import values_for

logger = logging.getLogger("scaffold")

# Fixed values that must never change at runtime (module constants, §17.1749).
PAPER_BASE_URL: str = "https://paper-api.alpaca.markets"
PAPER_DATA_URL: str = "https://data.alpaca.markets"
PAPER_STREAM_URL: str = "wss://paper-api.alpaca.markets/stream"
ALPACA_API_KEY_SECRET_NAME: str = "ALPACA_API_KEY_ID"
ALPACA_SECRET_KEY_SECRET_NAME: str = "ALPACA_API_SECRET_KEY"

# Valid order types and time-in-force values for Alpaca paper trading.
VALID_ORDER_TYPES: frozenset[str] = frozenset({"market", "limit", "stop", "stop_limit", "trailing_stop"})
VALID_TIME_IN_FORCE: frozenset[str] = frozenset({"day", "gtc", "opg", "cls", "ioc", "fok"})
VALID_ORDER_SIDES: frozenset[str] = frozenset({"buy", "sell"})

# Alpaca asset classes.
VALID_ASSET_CLASSES: frozenset[str] = frozenset({"us_equity", "us_option", "crypto"})


class AlpacaPaperClientError(Exception):
    """Raised when the Alpaca paper API returns an error or a call fails."""


class AlpacaSecretsMissingError(AlpacaPaperClientError):
    """Raised when the operator has not stored the Alpaca API secrets."""


@dataclass(frozen=True)
class AlpacaCredentials:
    """Alpaca paper trading credentials, read from the engine's secret store."""

    api_key_id: str
    secret_key: str


@dataclass(frozen=True)
class AlpacaOrder:
    """A submitted Alpaca paper order."""

    id: str
    client_order_id: str | None
    symbol: str
    side: str
    order_type: str
    qty: Decimal
    time_in_force: str
    limit_price: Decimal | None
    stop_price: Decimal | None
    status: str
    created_at: datetime
    submitted_at: datetime | None
    filled_at: datetime | None
    filled_qty: Decimal
    filled_avg_price: Decimal | None


@dataclass(frozen=True)
class AlpacaPosition:
    """An open Alpaca paper position."""

    symbol: str
    qty: Decimal
    avg_entry_price: Decimal
    market_value: Decimal | None
    unrealized_pl: Decimal | None
    current_price: Decimal | None
    side: str


@dataclass(frozen=True)
class AlpacaAccount:
    """Alpaca paper account summary."""

    id: str
    account_number: str
    status: str
    currency: str
    cash: Decimal
    equity: Decimal
    buying_power: Decimal
    portfolio_value: Decimal
    long_market_value: Decimal
    short_market_value: Decimal
    daytrade_count: int
    pattern_day_trader: bool
    trading_blocked: bool
    transfers_blocked: bool
    account_blocked: bool
    created_at: datetime


@dataclass(frozen=True)
class AlpacaClock:
    """Alpaca market clock."""

    timestamp: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime


async def _get_credentials(db: object) -> AlpacaCredentials:
    """Read Alpaca API secrets from the engine's secret store.

    Refuses to run (raises AlpacaSecretsMissingError) while either secret
    is missing. Never logs the values.
    """
    wanted = [ALPACA_API_KEY_SECRET_NAME, ALPACA_SECRET_KEY_SECRET_NAME]
    values = await values_for(db, wanted)
    api_key_id = values.get(ALPACA_API_KEY_SECRET_NAME)
    secret_key = values.get(ALPACA_SECRET_KEY_SECRET_NAME)
    if not api_key_id or not secret_key:
        missing = [
            name
            for name, val in (
                (ALPACA_API_KEY_SECRET_NAME, api_key_id),
                (ALPACA_SECRET_KEY_SECRET_NAME, secret_key),
            )
            if not val
        ]
        raise AlpacaSecretsMissingError(
            f"Missing Alpaca paper trading secrets: {', '.join(missing)}. "
            "Store them on the Machines page -> Secrets."
        )
    return AlpacaCredentials(api_key_id=api_key_id, secret_key=secret_key)


def _auth_headers(credentials: AlpacaCredentials) -> dict[str, str]:
    """Build Alpaca auth headers. Values are never logged."""
    return {
        "APCA-API-KEY-ID": credentials.api_key_id,
        "APCA-API-SECRET-KEY": credentials.secret_key,
    }


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse an ISO-8601 datetime string, or None."""
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        logger.warning("Could not parse datetime value: %r", value)
        return None


def _parse_decimal(value: Any) -> Decimal | None:
    """Parse a numeric value into a Decimal, or None."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (ValueError, TypeError, ArithmeticError):
        logger.warning("Could not parse decimal value: %r", value)
        return None


def _parse_order(data: dict[str, Any]) -> AlpacaOrder:
    """Parse an Alpaca order dict into an AlpacaOrder dataclass."""
    return AlpacaOrder(
        id=data["id"],
        client_order_id=data.get("client_order_id"),
        symbol=data["symbol"],
        side=data["side"],
        order_type=data["type"],
        qty=_parse_decimal(data.get("qty")) or Decimal("0"),
        time_in_force=data["time_in_force"],
        limit_price=_parse_decimal(data.get("limit_price")),
        stop_price=_parse_decimal(data.get("stop_price")),
        status=data["status"],
        created_at=_parse_datetime(data.get("created_at")) or datetime.now(timezone.utc),
        submitted_at=_parse_datetime(data.get("submitted_at")),
        filled_at=_parse_datetime(data.get("filled_at")),
        filled_qty=_parse_decimal(data.get("filled_qty")) or Decimal("0"),
        filled_avg_price=_parse_decimal(data.get("filled_avg_price")),
    )


def _parse_position(data: dict[str, Any]) -> AlpacaPosition:
    """Parse an Alpaca position dict into an AlpacaPosition dataclass."""
    return AlpacaPosition(
        symbol=data["symbol"],
        qty=_parse_decimal(data.get("qty")) or Decimal("0"),
        avg_entry_price=_parse_decimal(data.get("avg_entry_price")) or Decimal("0"),
        market_value=_parse_decimal(data.get("market_value")),
        unrealized_pl=_parse_decimal(data.get("unrealized_pl")),
        current_price=_parse_decimal(data.get("current_price")),
        side=data.get("side", "long"),
    )


def _parse_account(data: dict[str, Any]) -> AlpacaAccount:
    """Parse an Alpaca account dict into an AlpacaAccount dataclass."""
    return AlpacaAccount(
        id=data["id"],
        account_number=data.get("account_number", ""),
        status=data.get("status", "UNKNOWN"),
        currency=data.get("currency", "USD"),
        cash=_parse_decimal(data.get("cash")) or Decimal("0"),
        equity=_parse_decimal(data.get("equity")) or Decimal("0"),
        buying_power=_parse_decimal(data.get("buying_power")) or Decimal("0"),
        portfolio_value=_parse_decimal(data.get("portfolio_value")) or Decimal("0"),
        long_market_value=_parse_decimal(data.get("long_market_value")) or Decimal("0"),
        short_market_value=_parse_decimal(data.get("short_market_value")) or Decimal("0"),
        daytrade_count=int(data.get("daytrade_count", 0) or 0),
        pattern_day_trader=bool(data.get("pattern_day_trader", False)),
        trading_blocked=bool(data.get("trading_blocked", False)),
        transfers_blocked=bool(data.get("transfers_blocked", False)),
        account_blocked=bool(data.get("account_blocked", False)),
        created_at=_parse_datetime(data.get("created_at")) or datetime.now(timezone.utc),
    )


def _parse_clock(data: dict[str, Any]) -> AlpacaClock:
    """Parse an Alpaca clock dict into an AlpacaClock dataclass."""
    return AlpacaClock(
        timestamp=_parse_datetime(data.get("timestamp")) or datetime.now(timezone.utc),
        is_open=bool(data.get("is_open", False)),
        next_open=_parse_datetime(data.get("next_open")) or datetime.now(timezone.utc),
        next_close=_parse_datetime(data.get("next_close")) or datetime.now(timezone.utc),
    )


class AlpacaPaperClient:
    """Async client for the Alpaca paper trading sandbox API.

    All requests are sent to the paper endpoint (PAPER_BASE_URL). Secrets
    are read from the engine's secret store on first use and cached for the
    lifetime of the client instance.
    """

    def __init__(self, *, base_url: str = PAPER_BASE_URL, data_url: str = PAPER_DATA_URL) -> None:
        self._base_url = base_url.rstrip("/")
        self._data_url = data_url.rstrip("/")
        self._credentials: AlpacaCredentials | None = None
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Create (once) the shared httpx.AsyncClient for Alpaca calls."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def _ensure_credentials(self) -> AlpacaCredentials:
        """Load credentials from the engine's secret store (once)."""
        if self._credentials is None:
            async with async_session() as db:
                self._credentials = await _get_credentials(db)
        return self._credentials

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        base: str | None = None,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[Any]:
        """Perform an authenticated request against the Alpaca paper API.

        Raises AlpacaPaperClientError on HTTP errors or connection failures.
        """
        client = await self._ensure_client()
        credentials = await self._ensure_credentials()
        url_base = base or self._base_url
        url = f"{url_base}{path}"
        headers = _auth_headers(credentials)
        headers["Content-Type"] = "application/json"
        try:
            response = await client.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
            )
        except httpx.HTTPError as exc:
            raise AlpacaPaperClientError(f"Alpaca request failed: {exc}") from exc

        if response.status_code >= 400:
            raise AlpacaPaperClientError(
                f"Alpaca API error {response.status_code} for {method} {path}: {response.text[:500]}"
            )

        if response.status_code == 204:
            return {}

        try:
            return response.json()
        except ValueError as exc:
            raise AlpacaPaperClientError(
                f"Alpaca API returned non-JSON for {method} {path}: {response.text[:500]}"
            ) from exc

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    async def get_account(self) -> AlpacaAccount:
        """Return the paper account summary."""
        data = await self._request("GET", "/v2/account")
        if not isinstance(data, dict):
            raise AlpacaPaperClientError("Unexpected account response shape")
        return _parse_account(data)

    async def get_clock(self) -> AlpacaClock:
        """Return the current market clock."""
        data = await self._request("GET", "/v2/clock")
        if not isinstance(data, dict):
            raise AlpacaPaperClientError("Unexpected clock response shape")
        return _parse_clock(data)

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    async def submit_order(
        self,
        *,
        symbol: str,
        qty: Decimal | str,
        side: Literal["buy", "sell"],
        order_type: Literal["market", "limit", "stop", "stop_limit", "trailing_stop"],
        time_in_force: Literal["day", "gtc", "opg", "cls", "ioc", "fok"] = "day",
        limit_price: Decimal | str | None = None,
        stop_price: Decimal | str | None = None,
        client_order_id: str | None = None,
    ) -> AlpacaOrder:
        """Submit a paper order to Alpaca.

        Args:
            symbol: Ticker symbol (e.g. "AAPL").
            qty: Order quantity (positive).
            side: "buy" or "sell".
            order_type: One of the valid Alpaca order types.
            time_in_force: Time in force (default "day").
            limit_price: Required for limit/stop_limit orders.
            stop_price: Required for stop/stop_limit orders.
            client_order_id: Optional client-supplied idempotency key.

        Raises:
            ValueError: If arguments are invalid.
            AlpacaPaperClientError: If the API rejects the order.
        """
        if side not in VALID_ORDER_SIDES:
            raise ValueError(f"Invalid side {side!r}; must be one of {sorted(VALID_ORDER_SIDES)}")
        if order_type not in VALID_ORDER_TYPES:
            raise ValueError(f"Invalid order_type {order_type!r}; must be one of {sorted(VALID_ORDER_TYPES)}")
        if time_in_force not in VALID_TIME_IN_FORCE:
            raise ValueError(f"Invalid time_in_force {time_in_force!r}; must be one of {sorted(VALID_TIME_IN_FORCE)}")

        qty_dec = Decimal(str(qty))
        if qty_dec <= 0:
            raise ValueError("qty must be positive")

        body: dict[str, Any] = {
            "symbol": symbol,
            "qty": str(qty_dec),
            "side": side,
            "type": order_type,
            "time_in_force": time_in_force,
        }
        if client_order_id is not None:
            body["client_order_id"] = client_order_id
        if limit_price is not None:
            body["limit_price"] = str(limit_price)
        if stop_price is not None:
            body["stop_price"] = str(stop_price)

        data = await self._request("POST", "/v2/orders", json_body=body)
        if not isinstance(data, dict):
            raise AlpacaPaperClientError("Unexpected order response shape")
        return _parse_order(data)

    async def get_order(self, order_id: str) -> AlpacaOrder:
        """Fetch a single order by its Alpaca order id."""
        data = await self._request("GET", f"/v2/orders/{order_id}")
        if not isinstance(data, dict):
            raise AlpacaPaperClientError("Unexpected order response shape")
        return _parse_order(data)

    async def list_orders(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
        after: datetime | None = None,
        until: datetime | None = None,
        symbols: list[str] | None = None,
    ) -> list[AlpacaOrder]:
        """List paper orders, optionally filtered.

        Args:
            status: Filter by order status (e.g. "open", "closed", "all").
            limit: Maximum number of orders to return.
            after: Only orders submitted after this timestamp.
            until: Only orders submitted before this timestamp.
            symbols: Only orders for these symbols.
        """
        params: dict[str, Any] = {}
        if status is not None:
            params["status"] = status
        if limit is not None:
            params["limit"] = limit
        if after is not None:
            params["after"] = after.isoformat()
        if until is not None:
            params["until"] = until.isoformat()
        if symbols is not None:
            params["symbols"] = ",".join(symbols)

        data = await self._request("GET", "/v2/orders", params=params)
        if not isinstance(data, list):
            raise AlpacaPaperClientError("Unexpected orders response shape")
        return [_parse_order(item) for item in data if isinstance(item, dict)]

    async def cancel_order(self, order_id: str) -> None:
        """Cancel an open paper order."""
        await self._request("DELETE", f"/v2/orders/{order_id}")

    async def cancel_all_orders(self) -> None:
        """Cancel all open paper orders."""
        await self._request("DELETE", "/v2/orders")

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    async def get_position(self, symbol: str) -> AlpacaPosition:
        """Return the open position for a symbol."""
        data = await self._request("GET", f"/v2/positions/{symbol}")
        if not isinstance(data, dict):
            raise AlpacaPaperClientError("Unexpected position response shape")
        return _parse_position(data)

    async def list_positions(self) -> list[AlpacaPosition]:
        """Return all open positions."""
        data = await self._request("GET", "/v2/positions")
        if not isinstance(data, list):
            raise AlpacaPaperClientError("Unexpected positions response shape")
        return [_parse_position(item) for item in data if isinstance(item, dict)]

    async def close_position(self, symbol: str) -> None:
        """Close (liquidate) the position for a symbol."""
        await self._request("DELETE", f"/v2/positions/{symbol}")

    async def close_all_positions(self) -> None:
        """Close all open positions."""
        await self._request("DELETE", "/v2/positions")

    # ------------------------------------------------------------------
    # Assets
    # ------------------------------------------------------------------

    async def get_asset(self, symbol: str) -> dict[str, Any]:
        """Return asset information for a symbol."""
        data = await self._request("GET", f"/v2/assets/{symbol}")
        if not isinstance(data, dict):
            raise AlpacaPaperClientError("Unexpected asset response shape")
        return data

    async def list_assets(
        self,
        *,
        status: str | None = None,
        asset_class: str | None = None,
    ) -> list[dict[str, Any]]:
        """List assets, optionally filtered by status and asset class."""
        params: dict[str, Any] = {}
        if status is not None:
            params["status"] = status
        if asset_class is not None:
            if asset_class not in VALID_ASSET_CLASSES:
                raise ValueError(
                    f"Invalid asset_class {asset_class!r}; must be one of {sorted(VALID_ASSET_CLASSES)}"
                )
            params["asset_class"] = asset_class

        data = await self._request("GET", "/v2/assets", params=params)
        if not isinstance(data, list):
            raise AlpacaPaperClientError("Unexpected assets response shape")
        return [item for item in data if isinstance(item, dict)]

    # ------------------------------------------------------------------
    # Market data (paper data endpoint)
    # ------------------------------------------------------------------

    async def get_bars(
        self,
        symbol: str,
        *,
        timeframe: str = "1Day",
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch historical bars for a symbol from the paper data endpoint.

        Args:
            symbol: Ticker symbol.
            timeframe: Bar timeframe (e.g. "1Min", "1Hour", "1Day").
            start: Start timestamp (inclusive).
            end: End timestamp (inclusive).
            limit: Maximum number of bars to return.
        """
        params: dict[str, Any] = {
            "symbol": symbol,
            "timeframe": timeframe,
        }
        if start is not None:
            params["start"] = start.isoformat()
        if end is not None:
            params["end"] = end.isoformat()
        if limit is not None:
            params["limit"] = limit

        data = await self._request("GET", "/v2/stocks/bars", base=self._data_url, params=params)
        if not isinstance(data, dict):
            raise AlpacaPaperClientError("Unexpected bars response shape")
        bars = data.get("bars", [])
        return [item for item in bars if isinstance(item, dict)]

    async def get_latest_quote(self, symbol: str) -> dict[str, Any]:
        """Fetch the latest quote for a symbol from the paper data endpoint."""
        data = await self._request(
            "GET", "/v2/stocks/quotes/latest", base=self._data_url, params={"symbols": symbol}
        )
        if not isinstance(data, dict):
            raise AlpacaPaperClientError("Unexpected quote response shape")
        return data

    async def get_latest_trade(self, symbol: str) -> dict[str, Any]:
        """Fetch the latest trade for a symbol from the paper data endpoint."""
        data = await self._request(
            "GET", "/v2/stocks/trades/latest", base=self._data_url, params={"symbols": symbol}
        )
        if not isinstance(data, dict):
            raise AlpacaPaperClientError("Unexpected trade response shape")
        return data
