from unittest import mock
from typing import Union, Optional

import pytest
from llama_index.core.workflow.workflow import (
    Workflow,
    Context,
)
from llama_index.core.workflow.decorators import step
from llama_index.core.workflow.errors import WorkflowRuntimeError
from llama_index.core.workflow.events import StartEvent, StopEvent, Event
from llama_index.core.workflow.workflow import Workflow

from .conftest import OneTestEvent, AnotherTestEvent


@pytest.mark.asyncio()
async def test_collect_events():
    ev1 = OneTestEvent()
    ev2 = AnotherTestEvent()

    class TestWorkflow(Workflow):
        @step
        async def step1(self, _: StartEvent) -> OneTestEvent:
            return ev1

        @step
        async def step2(self, _: StartEvent) -> AnotherTestEvent:
            return ev2

        @step
        async def step3(
            self, ctx: Context, ev: Union[OneTestEvent, AnotherTestEvent]
        ) -> Optional[StopEvent]:
            events = ctx.collect_events(ev, [OneTestEvent, AnotherTestEvent])
            if events is None:
                return None
            return StopEvent(result=events)

    workflow = TestWorkflow()
    result = await workflow.run()
    assert result == [ev1, ev2]


@pytest.mark.asyncio()
async def test_get_default(workflow):
    c1 = Context(workflow)
    assert await c1.get(key="test_key", default=42) == 42


@pytest.mark.asyncio()
async def test_get(ctx):
    await ctx.set("foo", 42)
    assert await ctx.get("foo") == 42


@pytest.mark.asyncio()
async def test_get_not_found(ctx):
    with pytest.raises(ValueError):
        await ctx.get("foo")


@pytest.mark.asyncio()
async def test_legacy_data(workflow):
    c1 = Context(workflow)
    await c1.set(key="test_key", value=42)
    assert c1.data["test_key"] == 42


def test_send_event_step_is_none(ctx):
    ctx._queues = {"step1": mock.MagicMock(), "step2": mock.MagicMock()}
    ev = Event(foo="bar")
    ctx.send_event(ev)
    for q in ctx._queues.values():
        q.put_nowait.assert_called_with(ev)
    assert ctx._broker_log == [ev]


def test_send_event_to_non_existent_step(ctx):
    with pytest.raises(
        WorkflowRuntimeError, match="Step does_not_exist does not exist"
    ):
        ctx.send_event(Event(), "does_not_exist")


def test_send_event_to_wrong_step(ctx):
    ctx._workflow._get_steps = mock.MagicMock(return_value={"step": mock.MagicMock()})

    with pytest.raises(
        WorkflowRuntimeError,
        match="Step step does not accept event of type <class 'llama_index.core.workflow.events.Event'>",
    ):
        ctx.send_event(Event(), "step")


def test_send_event_to_step(ctx):
    step2 = mock.MagicMock()
    step2.__step_config.accepted_events = [Event]

    ctx._workflow._get_steps = mock.MagicMock(
        return_value={"step1": mock.MagicMock(), "step2": step2}
    )
    ctx._queues = {"step1": mock.MagicMock(), "step2": mock.MagicMock()}

    ev = Event(foo="bar")
    ctx.send_event(ev, "step2")

    ctx._queues["step1"].put_nowait.assert_not_called()
    ctx._queues["step2"].put_nowait.assert_called_with(ev)


def test_get_result(ctx):
    ctx._retval = 42
    assert ctx.get_result() == 42


@pytest.mark.asyncio()
async def test_deprecated_params(ctx):
    with pytest.warns(
        DeprecationWarning, match="`make_private` is deprecated and will be ignored"
    ):
        await ctx.set("foo", 42, make_private=True)