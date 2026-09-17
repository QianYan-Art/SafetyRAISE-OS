import asyncio

from app.report_harness.lifecycle import reconcile_loop


def test_cleanup_loop_runs_immediately_and_stops_without_another_sweep():
    async def exercise():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        class Recovery:
            calls = 0

            def sweep_expired(self):
                self.calls += 1
                loop.call_soon_threadsafe(stop.set)

        recovery = Recovery()
        await asyncio.wait_for(reconcile_loop(recovery, stop, interval=0.01), 2)
        assert recovery.calls == 1

    asyncio.run(exercise())


def test_cleanup_failure_retries_without_logging_connection_details(caplog):
    async def exercise():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        class Recovery:
            calls = 0

            def sweep_expired(self):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("private-connection-details")
                loop.call_soon_threadsafe(stop.set)

        recovery = Recovery()
        await asyncio.wait_for(reconcile_loop(recovery, stop, interval=0.01), 2)
        assert recovery.calls == 2

    asyncio.run(exercise())
    assert "private-connection-details" not in caplog.text
    assert "本地对账失败" in caplog.text
