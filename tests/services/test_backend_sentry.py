"""Tests for SentryBackend.

Uses httpx.MockTransport to drive the client without a network. Each
test pins a request handler that asserts on method/path/headers/body
and returns a constructed httpx.Response.
"""

import json

import httpx
import pytest

from app.services.backend import (
    AlreadyShippedError,
    BackendError,
    IdempotencyLockTimeoutError,
    IdempotencyMismatchError,
    InvalidBodyError,
    ItemData,
    NetworkError,
    NotFoundError,
    NotInShippableStatusError,
    NotShippedError,
    OrderData,
    ShipResult,
    UnknownOperatorError,
    VoidResult,
)
from app.services.backend.sentry import SentryBackend


BASE_URL = "https://sentry.test"


def _make_backend(handler, *, token: str = "wms_t_unit-test-token") -> SentryBackend:
    transport = httpx.MockTransport(handler)
    client = httpx.Client(base_url=BASE_URL, transport=transport, timeout=5.0)
    return SentryBackend(base_url=BASE_URL, get_token=lambda: token, client=client)


def _ok(body):
    return httpx.Response(200, json=body, headers={"X-Sentry-Canonical-Model": "DRAFT-v1"})


def _err(status, error_kind, message="", details=None):
    return httpx.Response(
        status,
        json={
            "error_kind": error_kind,
            "message": message or error_kind,
            "details": details or {},
        },
        headers={"X-Sentry-Canonical-Model": "DRAFT-v1"},
    )


# ----- get_order ---------------------------------------------------------


class TestGetOrder:

    def test_happy_path(self):
        def handler(request):
            assert request.method == "GET"
            assert request.url.path == "/api/v1/dockd/orders/SO-1001"
            assert request.headers.get("X-WMS-Token") == "wms_t_unit-test-token"
            assert request.headers.get("Accept") == "application/json"
            return _ok({
                "so_number": "SO-1001",
                "external_id": "00000000-0000-0000-0000-000000000001",
                "status": "PICKED",
                "warehouse_id": 7,
                "customer_name": "Pat Q. Customer",
                "customer_phone": "555-0100",
                "memo": "leave at side door",
                "shipping_address": {
                    "name": "Pat Q. Customer",
                    "line1": "123 Main St",
                    "line2": "Apt 4",
                    "city": "Denver",
                    "state": "CO",
                    "postal_code": "80202",
                    "country": "US",
                    "phone": "555-0100",
                },
                "ship_method": "USPS Ground Advantage",
                "items": [
                    {
                        "external_id": "00000000-0000-0000-0000-000000000099",
                        "sku": "WIDGET-A",
                        "display_name": "Blue Widget",
                        "upc": "012345678905",
                        "qty": 2,
                    },
                ],
                "order_total": 45.67,
                "customer_shipping_paid": 3.50,
                "marketplace": "amazon",
                "order_date": "2026-05-10T12:00:00+00:00",
                "ff_created_at": "2026-05-10T12:34:56+00:00",
                "shippable": True,
                "shippable_from_statuses": ["PICKED", "PACKED"],
                "shipped_by": None,
                "tracking_number": None,
                "carrier": None,
                "shipped_at": None,
                "station_label": None,
            })

        backend = _make_backend(handler)
        order = backend.get_order("SO-1001")

        assert isinstance(order, OrderData)
        assert order.so_number == "SO-1001"
        assert order.warehouse_id == 7
        assert order.shippable is True
        assert order.shippable_from_statuses == ["PICKED", "PACKED"]
        assert order.shipping_address.line1 == "123 Main St"
        assert order.shipping_address.postal_code == "80202"
        assert len(order.items) == 1
        assert order.items[0].sku == "WIDGET-A"
        assert order.items[0].qty == 2
        assert order.order_total == 45.67
        assert order.customer_shipping_paid == 3.50
        assert order.marketplace == "amazon"
        assert order.memo == "leave at side door"

    def test_404_raises_not_found(self):
        def handler(request):
            return _err(404, "not_found", "order not found")

        backend = _make_backend(handler)
        with pytest.raises(NotFoundError) as exc_info:
            backend.get_order("SO-MISSING")
        assert exc_info.value.error_kind == "not_found"
        assert exc_info.value.status_code == 404

    def test_already_shipped_passes_details(self):
        """Even GET can technically surface shipped fields; the
        AlreadyShippedError path is exercised against the POST /ship
        route, but the 409 mapping is the same code path."""
        def handler(request):
            return _err(409, "already_shipped", "shipped", {
                "existing_tracking": "1Z999",
                "carrier": "UPS",
            })

        backend = _make_backend(handler)
        with pytest.raises(AlreadyShippedError) as exc_info:
            backend.get_order("SO-1001")
        assert exc_info.value.details["existing_tracking"] == "1Z999"

    def test_timeout_raises_network_error(self):
        def handler(request):
            raise httpx.TimeoutException("read timeout")

        backend = _make_backend(handler)
        with pytest.raises(NetworkError):
            backend.get_order("SO-1001")


# ----- lookup_item -------------------------------------------------------


class TestLookupItem:

    def test_happy_path_aggregates_quantity_across_locations(self):
        def handler(request):
            assert request.method == "GET"
            assert request.url.path == "/api/v1/dockd/items/053526423167"
            assert request.headers.get("X-WMS-Token") == "wms_t_unit-test-token"
            return _ok({
                "item": {
                    "item_id": 42,
                    "sku": "1264-42316",
                    "item_name": "Antron Yarn Black",
                    "upc": "053526423167",
                    "category": "thread",
                    "weight_lbs": 0.05,
                },
                "locations": [
                    {"bin_id": 1, "bin_code": "A1-01", "quantity_on_hand": 20},
                    {"bin_id": 2, "bin_code": "A1-02", "quantity_on_hand": 17},
                ],
            })

        backend = _make_backend(handler)
        item = backend.lookup_item("053526423167")
        assert isinstance(item, ItemData)
        assert item.sku == "1264-42316"
        assert item.upc == "053526423167"
        assert item.quantity_on_hand == 37

    def test_returns_zero_quantity_when_no_locations(self):
        def handler(request):
            return _ok({
                "item": {"item_id": 7, "sku": "X", "item_name": "n", "upc": None},
                "locations": [],
            })

        backend = _make_backend(handler)
        item = backend.lookup_item("X")
        assert item.upc is None
        assert item.quantity_on_hand == 0

    def test_404_raises_not_found(self):
        def handler(request):
            return _err(404, "not_found", "item not found")

        backend = _make_backend(handler)
        with pytest.raises(NotFoundError):
            backend.lookup_item("UNKNOWN")


# ----- confirm_shipped ---------------------------------------------------


class TestConfirmShipped:

    def test_happy_path(self):
        captured = {}

        def handler(request):
            assert request.method == "POST"
            assert request.url.path == "/api/v1/dockd/orders/SO-1001/ship"
            body = json.loads(request.content.decode())
            captured["body"] = body
            return _ok({
                "status": "SHIPPED",
                "tracking": body["tracking"],
                "shipped_at": "2026-05-11T15:30:00Z",
                "fulfillment_id": 42,
                "audit_log_id": 9001,
            })

        backend = _make_backend(handler)
        result = backend.confirm_shipped(
            "SO-1001",
            tracking="1Z999AA10123456784",
            carrier="UPS",
            ship_method="UPS - Ground",
            operator_username="alice",
            shipping_cost=8.75,
            weight=1.5,
            dims={"l": 8, "w": 6, "h": 4},
            manual_link=False,
            idempotency_key="550e8400-e29b-41d4-a716-446655440000",
        )
        assert isinstance(result, ShipResult)
        assert result.fulfillment_id == 42
        assert result.audit_log_id == 9001
        assert result.tracking == "1Z999AA10123456784"

        body = captured["body"]
        assert body["tracking"] == "1Z999AA10123456784"
        assert body["carrier"] == "UPS"
        assert body["ship_method"] == "UPS - Ground"
        assert body["operator_username"] == "alice"
        assert body["shipping_cost"] == 8.75
        assert body["weight"] == 1.5
        assert body["dims"] == {"l": 8.0, "w": 6.0, "h": 4.0}
        assert body["manual_link"] is False
        assert body["idempotency_key"] == "550e8400-e29b-41d4-a716-446655440000"

    def test_omits_optional_fields_when_none(self):
        """Sentry's ShipBody is extra='forbid' but the optional fields
        accept null. Either omitting or sending None is equivalent for
        the wire; we omit to keep payloads tight."""
        captured = {}

        def handler(request):
            captured["body"] = json.loads(request.content.decode())
            return _ok({
                "status": "SHIPPED",
                "tracking": "T",
                "shipped_at": "2026-05-11T00:00:00Z",
                "fulfillment_id": 1,
                "audit_log_id": 1,
            })

        backend = _make_backend(handler)
        backend.confirm_shipped(
            "SO-2",
            tracking="T",
            carrier="USPS",
            ship_method=None,
            operator_username="bob",
            shipping_cost=None,
            weight=None,
            dims=None,
            manual_link=True,
            idempotency_key="550e8400-e29b-41d4-a716-446655440001",
        )
        body = captured["body"]
        assert "ship_method" not in body
        assert "shipping_cost" not in body
        assert "weight" not in body
        assert "dims" not in body
        assert body["manual_link"] is True

    def test_already_shipped_error(self):
        def handler(request):
            return _err(409, "already_shipped", "shipped", {
                "existing_tracking": "1Z999AA10123456784",
                "carrier": "UPS",
                "shipped_at": "2026-05-10T10:00:00Z",
                "shipped_by": "alice",
                "station_label": None,
            })

        backend = _make_backend(handler)
        with pytest.raises(AlreadyShippedError) as exc_info:
            backend.confirm_shipped(
                "SO-1",
                tracking="T", carrier="UPS", ship_method=None,
                operator_username="bob",
                shipping_cost=None, weight=None, dims=None,
                manual_link=False,
                idempotency_key="550e8400-e29b-41d4-a716-446655440002",
            )
        assert exc_info.value.details["existing_tracking"] == "1Z999AA10123456784"
        assert exc_info.value.details["shipped_by"] == "alice"

    def test_not_in_shippable_status(self):
        def handler(request):
            return _err(410, "not_in_shippable_status", "not yet picked", {
                "current_status": "OPEN",
                "allowed_statuses": ["PICKED", "PACKED"],
            })

        backend = _make_backend(handler)
        with pytest.raises(NotInShippableStatusError) as exc_info:
            backend.confirm_shipped(
                "SO-1", tracking="T", carrier="UPS", ship_method=None,
                operator_username="bob", shipping_cost=None,
                weight=None, dims=None, manual_link=False,
                idempotency_key="550e8400-e29b-41d4-a716-446655440003",
            )
        assert exc_info.value.details["current_status"] == "OPEN"

    def test_idempotency_mismatch(self):
        def handler(request):
            return _err(409, "idempotency_key_reused_with_different_body")

        backend = _make_backend(handler)
        with pytest.raises(IdempotencyMismatchError):
            backend.confirm_shipped(
                "SO-1", tracking="T", carrier="UPS", ship_method=None,
                operator_username="bob", shipping_cost=None,
                weight=None, dims=None, manual_link=False,
                idempotency_key="550e8400-e29b-41d4-a716-446655440004",
            )

    def test_idempotency_lock_timeout(self):
        def handler(request):
            return _err(503, "idempotency_lock_timeout", "retry")

        backend = _make_backend(handler)
        with pytest.raises(IdempotencyLockTimeoutError):
            backend.confirm_shipped(
                "SO-1", tracking="T", carrier="UPS", ship_method=None,
                operator_username="bob", shipping_cost=None,
                weight=None, dims=None, manual_link=False,
                idempotency_key="550e8400-e29b-41d4-a716-446655440005",
            )

    def test_unknown_operator(self):
        def handler(request):
            return _err(422, "unknown_operator", "operator not found",
                        {"field": "operator_username"})

        backend = _make_backend(handler)
        with pytest.raises(UnknownOperatorError) as exc_info:
            backend.confirm_shipped(
                "SO-1", tracking="T", carrier="UPS", ship_method=None,
                operator_username="ghost", shipping_cost=None,
                weight=None, dims=None, manual_link=False,
                idempotency_key="550e8400-e29b-41d4-a716-446655440006",
            )
        assert exc_info.value.details["field"] == "operator_username"

    def test_invalid_body(self):
        def handler(request):
            return _err(422, "invalid_body", "validation failed",
                        {"field": "tracking", "reason": "string_too_short"})

        backend = _make_backend(handler)
        with pytest.raises(InvalidBodyError):
            backend.confirm_shipped(
                "SO-1", tracking="", carrier="UPS", ship_method=None,
                operator_username="bob", shipping_cost=None,
                weight=None, dims=None, manual_link=False,
                idempotency_key="550e8400-e29b-41d4-a716-446655440007",
            )

    def test_500_raises_network_error(self):
        def handler(request):
            return _err(500, "internal_error", "boom")

        backend = _make_backend(handler)
        with pytest.raises(NetworkError):
            backend.confirm_shipped(
                "SO-1", tracking="T", carrier="UPS", ship_method=None,
                operator_username="bob", shipping_cost=None,
                weight=None, dims=None, manual_link=False,
                idempotency_key="550e8400-e29b-41d4-a716-446655440008",
            )

    def test_unmapped_status_raises_generic(self):
        def handler(request):
            return _err(418, "i_am_a_teapot", "no coffee here")

        backend = _make_backend(handler)
        with pytest.raises(BackendError) as exc_info:
            backend.confirm_shipped(
                "SO-1", tracking="T", carrier="UPS", ship_method=None,
                operator_username="bob", shipping_cost=None,
                weight=None, dims=None, manual_link=False,
                idempotency_key="550e8400-e29b-41d4-a716-446655440009",
            )
        assert exc_info.value.error_kind == "i_am_a_teapot"
        assert exc_info.value.status_code == 418


# ----- void_ship ---------------------------------------------------------


class TestVoidShip:

    def test_happy_path(self):
        captured = {}

        def handler(request):
            assert request.method == "POST"
            assert request.url.path == "/api/v1/dockd/orders/SO-1001/void-ship"
            captured["body"] = json.loads(request.content.decode())
            return _ok({
                "status": "PACKED",
                "voided_at": "2026-05-11T16:00:00Z",
                "audit_log_id": 9100,
            })

        backend = _make_backend(handler)
        result = backend.void_ship(
            "SO-1001",
            reason="wrong box dims",
            operator_username="alice",
            idempotency_key="550e8400-e29b-41d4-a716-446655440010",
        )
        assert isinstance(result, VoidResult)
        assert result.status == "PACKED"
        assert result.audit_log_id == 9100
        assert captured["body"]["reason"] == "wrong box dims"

    def test_not_shipped(self):
        def handler(request):
            return _err(409, "not_shipped", "not in SHIPPED status",
                        {"current_status": "PICKED"})

        backend = _make_backend(handler)
        with pytest.raises(NotShippedError) as exc_info:
            backend.void_ship(
                "SO-1",
                reason="x",
                operator_username="bob",
                idempotency_key="550e8400-e29b-41d4-a716-446655440011",
            )
        assert exc_info.value.details["current_status"] == "PICKED"


# ----- health ------------------------------------------------------------


class TestHealth:

    def test_healthy_returns_true(self):
        def handler(request):
            assert request.url.path == "/api/health"
            return httpx.Response(200, json={"status": "ok"})

        backend = _make_backend(handler)
        assert backend.health() is True

    def test_unhealthy_status_returns_false(self):
        def handler(request):
            return httpx.Response(503, json={"status": "down"})

        backend = _make_backend(handler)
        assert backend.health() is False

    def test_timeout_returns_false(self):
        def handler(request):
            raise httpx.TimeoutException("timeout")

        backend = _make_backend(handler)
        assert backend.health() is False


# ----- construction sanity -----------------------------------------------


class TestConstruction:

    def test_empty_base_url_rejected(self):
        with pytest.raises(ValueError):
            SentryBackend(base_url="", get_token=lambda: "x")

    def test_base_url_trailing_slash_stripped(self):
        def handler(request):
            assert str(request.url).startswith(BASE_URL + "/api/")
            return _ok({"so_number": "S", "external_id": "e", "status": "PICKED",
                        "warehouse_id": 1, "items": [], "shipping_address": {},
                        "shippable": True, "shippable_from_statuses": []})

        transport = httpx.MockTransport(handler)
        client = httpx.Client(base_url=BASE_URL + "/", transport=transport)
        backend = SentryBackend(base_url=BASE_URL + "/", get_token=lambda: "t", client=client)
        backend.get_order("S")
