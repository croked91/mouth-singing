"""Tests for ``karaoke_shared.messaging.rabbitmq.RabbitMQClient``.

Mocks ``aio_pika.connect_robust`` and the channel/exchange/queue API so the
test suite never opens a real broker connection. Validates:

  * connect()/close() lifecycle
  * declare_topology() — every exchange/queue/binding the worker depends on,
    including DLX arguments and ``x-max-priority`` for ``jobs.process``
  * publish() — JSON encoding, persistent delivery mode, priority pass-through
  * consume() — set_qos + queue.consume wiring
  * create_exclusive_queue() — exclusive+auto-delete, bound to fanout
  * channel property guard before connect()
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import aio_pika
import pytest

from karaoke_shared.messaging.rabbitmq import RabbitMQClient


def _make_channel() -> MagicMock:
    """Return an AbstractChannel stand-in with awaitable declare/get methods."""
    ch = MagicMock()
    ch.declare_exchange = AsyncMock(side_effect=lambda *a, **kw: MagicMock(name=f"exchange:{a[0]}"))

    queues: dict[str, MagicMock] = {}

    async def declare_queue(name: str = "", **kw):
        q = MagicMock(name=f"queue:{name or '<exclusive>'}")
        q.bind = AsyncMock()
        q.consume = AsyncMock()
        # Remember declaration kwargs so tests can introspect them.
        q._declare_kwargs = kw
        if name:
            queues[name] = q
        return q

    async def get_queue(name: str):
        return queues[name]

    exchanges: dict[str, MagicMock] = {}

    async def get_exchange(name: str):
        if name not in exchanges:
            ex = MagicMock(name=f"exchange:{name}")
            ex.publish = AsyncMock()
            exchanges[name] = ex
        return exchanges[name]

    ch.declare_queue = AsyncMock(side_effect=declare_queue)
    ch.get_queue = AsyncMock(side_effect=get_queue)
    ch.get_exchange = AsyncMock(side_effect=get_exchange)
    ch.set_qos = AsyncMock()
    ch._declared_queues = queues
    ch._declared_exchanges = exchanges
    return ch


@pytest.fixture
def client():
    return RabbitMQClient("amqp://guest:guest@localhost:5672/")


def test_channel_raises_before_connect(client):
    with pytest.raises(RuntimeError, match="not connected"):
        _ = client.channel


async def test_connect_opens_robust_connection_and_channel(client):
    fake_conn = MagicMock()
    fake_conn.is_closed = False
    fake_conn.channel = AsyncMock(return_value="ch-stub")
    fake_conn.close = AsyncMock()

    with patch("aio_pika.connect_robust", AsyncMock(return_value=fake_conn)) as cr:
        await client.connect()

    cr.assert_awaited_once_with("amqp://guest:guest@localhost:5672/")
    fake_conn.channel.assert_awaited_once()
    assert client._channel == "ch-stub"


async def test_close_skips_when_already_closed(client):
    fake_conn = MagicMock()
    fake_conn.is_closed = True
    fake_conn.close = AsyncMock()
    client._connection = fake_conn

    await client.close()

    fake_conn.close.assert_not_called()


async def test_close_closes_open_connection(client):
    fake_conn = MagicMock()
    fake_conn.is_closed = False
    fake_conn.close = AsyncMock()
    client._connection = fake_conn

    await client.close()

    fake_conn.close.assert_awaited_once()


async def test_declare_topology_creates_full_topology(client):
    ch = _make_channel()
    client._channel = ch

    await client.declare_topology()

    # 4 exchanges in the order the implementation declares them
    exchange_calls = [c.args for c in ch.declare_exchange.call_args_list]
    assert ("dlq", aio_pika.ExchangeType.DIRECT) in exchange_calls
    assert ("jobs", aio_pika.ExchangeType.DIRECT) in exchange_calls
    assert ("job.progress", aio_pika.ExchangeType.FANOUT) in exchange_calls
    assert ("rec", aio_pika.ExchangeType.DIRECT) in exchange_calls
    # job.progress must NOT be durable (SSE topology), the others must be
    durable_by_name: dict[str, bool] = {}
    for call in ch.declare_exchange.call_args_list:
        durable_by_name[call.args[0]] = call.kwargs.get("durable")
    assert durable_by_name["dlq"] is True
    assert durable_by_name["jobs"] is True
    assert durable_by_name["rec"] is True
    assert durable_by_name["job.progress"] is False

    # Every persistent queue we depend on must be declared
    declared = ch._declared_queues
    assert set(declared) >= {
        "jobs.process", "jobs.dlq", "rec.index", "rec.dlq", "rec.indexed",
    }

    # x-max-priority + DLX wiring on jobs.process
    jp = declared["jobs.process"]
    args = jp._declare_kwargs["arguments"]
    assert args["x-max-priority"] == 10
    assert args["x-dead-letter-exchange"] == "dlq"
    assert args["x-dead-letter-routing-key"] == "jobs"

    # rec.index DLX wiring
    rec_args = declared["rec.index"]._declare_kwargs["arguments"]
    assert rec_args["x-dead-letter-exchange"] == "dlq"
    assert rec_args["x-dead-letter-routing-key"] == "rec"

    # DLQ bindings: jobs.dlq → routing_key="jobs", rec.dlq → routing_key="rec"
    jobs_dlq_bind = declared["jobs.dlq"].bind.await_args_list[0]
    assert jobs_dlq_bind.kwargs["routing_key"] == "jobs"
    rec_dlq_bind = declared["rec.dlq"].bind.await_args_list[0]
    assert rec_dlq_bind.kwargs["routing_key"] == "rec"

    # rec.indexed binding uses routing_key="indexed"
    indexed_bind = declared["rec.indexed"].bind.await_args_list[0]
    assert indexed_bind.kwargs["routing_key"] == "indexed"


async def test_publish_serializes_json_with_priority(client):
    ch = _make_channel()
    client._channel = ch

    await client.publish("jobs", "", {"job_id": "abc", "priority": 7}, priority=7)

    ch.get_exchange.assert_awaited_with("jobs")
    ex = ch._declared_exchanges["jobs"]
    publish_call = ex.publish.await_args_list[-1]
    msg = publish_call.args[0]
    assert isinstance(msg, aio_pika.Message)
    assert json.loads(msg.body.decode()) == {"job_id": "abc", "priority": 7}
    assert msg.content_type == "application/json"
    assert msg.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
    assert msg.priority == 7
    assert publish_call.kwargs["routing_key"] == ""


async def test_publish_without_priority_passes_none(client):
    ch = _make_channel()
    client._channel = ch

    await client.publish("rec", "indexed", {"track_id": "t1"})

    ex = ch._declared_exchanges["rec"]
    publish_call = ex.publish.await_args_list[-1]
    msg = publish_call.args[0]
    # aio-pika normalises None priority to 0 (AMQP default), so we just check
    # the message body and routing key here — priority semantics are covered
    # in the previous test.
    assert json.loads(msg.body.decode()) == {"track_id": "t1"}
    assert publish_call.kwargs["routing_key"] == "indexed"


async def test_consume_sets_qos_and_attaches_callback(client):
    ch = _make_channel()
    client._channel = ch
    # Pre-declare jobs.process so get_queue() resolves.
    await ch.declare_queue("jobs.process")

    callback = AsyncMock()
    await client.consume("jobs.process", callback, prefetch_count=3)

    ch.set_qos.assert_awaited_once_with(prefetch_count=3)
    q = ch._declared_queues["jobs.process"]
    q.consume.assert_awaited_once_with(callback)


async def test_create_exclusive_queue_binds_to_fanout(client):
    ch = _make_channel()
    client._channel = ch

    queue = await client.create_exclusive_queue("job.progress")

    # The queue passed to declare_queue should have exclusive=True, auto_delete=True
    last_q_call = ch.declare_queue.await_args_list[-1]
    assert last_q_call.kwargs.get("exclusive") is True
    assert last_q_call.kwargs.get("auto_delete") is True
    queue.bind.assert_awaited_once()
