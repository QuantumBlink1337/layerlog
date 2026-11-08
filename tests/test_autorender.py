import json
import threading
import time
from pathlib import Path

from layerlog.bambuddy import PrinterStatus
from layerlog.surveillance.autorender import END_GRACE_S, RenderScheduler, RenderWorker, is_paused

FIXTURES = Path(__file__).parent / "fixtures"
STATUS = json.loads((FIXTURES / "printer_1_status_printing.json").read_text())


def status(**over) -> PrinterStatus:
    return PrinterStatus.from_json({**STATUS, **over})


def make():
    t = [0.0]
    fired: list[tuple[int, str, bool]] = []
    sched = RenderScheduler(lambda a, r, f: fired.append((a, r, f)), interval=3600, clock=lambda: t[0])
    return sched, fired, t


def test_hourly_while_printing():
    sched, fired, t = make()
    for t[0] in (0, 1800, 3599):
        sched.observe(status())
    assert fired == []
    t[0] = 3600
    sched.observe(status())
    t[0] = 3700
    sched.observe(status())
    assert fired == [(27, "hourly", False)]


def test_pause_renders_once_per_pause_by_state_or_stage():
    sched, fired, t = make()
    sched.observe(status())
    t[0] = 10
    sched.observe(status(state="PAUSE", stg_cur_name="Paused by the user"))
    sched.observe(status(state="PAUSE", stg_cur_name="Paused by the user"))
    sched.observe(status())
    sched.observe(status(stg_cur_name="Pause due to spaghetti detected"))
    assert fired == [(27, "paused (Paused by the user)", False),
                     (27, "paused (Pause due to spaghetti detected)", False)]


def test_pause_resets_hourly_clock():
    sched, fired, t = make()
    sched.observe(status())
    t[0] = 3000
    sched.observe(status(state="PAUSE"))
    t[0] = 3600
    sched.observe(status())
    assert [r for _, r, _ in fired] == ["paused (Printing)"]


def test_end_is_final_only_after_grace_period():
    sched, fired, t = make()
    sched.observe(status())
    t[0] = 100
    sched.observe(status(state="FINISH"))
    t[0] = 100 + END_GRACE_S - 1
    sched.observe(status(state="FINISH"))
    assert fired == []
    t[0] = 100 + END_GRACE_S
    sched.observe(status(state="FINISH"))
    sched.observe(status(state="FINISH"))
    assert fired == [(27, "print ended (FINISH)", True)]


def test_status_blip_mid_print_is_not_an_end():
    sched, fired, t = make()
    sched.observe(status())
    t[0] = 100
    sched.observe(status(state="IDLE"))
    t[0] = 102
    sched.observe(status())
    t[0] = 100 + END_GRACE_S + 5
    sched.observe(status(state="IDLE"))
    assert fired == []


def test_new_archive_finalizes_the_previous_one():
    sched, fired, t = make()
    sched.observe(status())
    t[0] = 10
    sched.observe(status(current_archive_id=28))
    assert fired == [(27, "print ended", True)]


def test_paused_matches_state_or_stage():
    assert is_paused(status(state="PAUSE", stg_cur_name=None))
    assert is_paused(status(stg_cur_name="Paused due to filament runout"))
    assert not is_paused(status())


def wait_idle(worker: RenderWorker) -> None:
    deadline = time.monotonic() + 5
    while not worker.idle():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_worker_coalesces_requests_and_keeps_final_flag():
    gate = threading.Event()
    calls = []

    def render(archive_id, final):
        gate.wait(5)
        calls.append((archive_id, final))
        return f"archive_{archive_id}.mp4"

    worker = RenderWorker(render)
    worker.request(27, "hourly")  # starts, blocks on the gate
    time.sleep(0.05)
    worker.request(27, "paused")
    worker.request(27, "print ended", True)
    gate.set()
    wait_idle(worker)
    assert calls == [(27, False), (27, True)]


def test_worker_survives_a_failed_render():
    calls = []

    def render(archive_id, final):
        calls.append(archive_id)
        if archive_id == 1:
            raise RuntimeError("ffmpeg blew up")
        return "ok"

    worker = RenderWorker(render)
    worker.request(1, "hourly")
    worker.request(2, "hourly")
    wait_idle(worker)
    assert calls == [1, 2]


class FakeClient:
    def __init__(self, active: PrinterStatus, archives: dict[int, str]):
        self.active, self.archives = active, archives

    def printer_status(self, printer_id):
        return self.active

    def get_archive(self, archive_id):
        from layerlog.bambuddy import ArchiveNotFound
        if archive_id not in self.archives:
            raise ArchiveNotFound(f"archive {archive_id}", 404)
        return type("A", (), {"status": self.archives[archive_id]})()


def raw_dirs(tmp_path: Path, *ids: int) -> Path:
    for i in ids:
        (tmp_path / f"archive_{i}").mkdir()
    (tmp_path / "stray.txt").write_text("")
    (tmp_path / "archive_x").mkdir()
    return tmp_path


def test_recover_queues_only_ended_non_active_archives(tmp_path):
    from layerlog.surveillance.autorender import recover_missed_ends
    fired = []
    client = FakeClient(status(), {20: "completed", 21: "failed", 22: "printing", 27: "printing"})
    queued = recover_missed_ends(client, 1, raw_dirs(tmp_path, 20, 21, 22, 23, 27),
                                 lambda a, r, f: fired.append((a, r, f)))
    # 22: Bambuddy says printing; 23: unknown to Bambuddy; 27: the active print.
    assert queued == [20, 21]
    assert fired == [(20, "missed end (completed)", True), (21, "missed end (failed)", True)]


def test_recover_when_idle_still_requires_bambuddy_to_agree(tmp_path):
    from layerlog.surveillance.autorender import recover_missed_ends
    fired = []
    client = FakeClient(status(state="FINISH"), {27: "completed", 28: "printing"})
    assert recover_missed_ends(client, 1, raw_dirs(tmp_path, 27, 28),
                               lambda a, r, f: fired.append(a)) == [27]


def test_recover_does_nothing_without_printer_status(tmp_path):
    from layerlog.bambuddy import BambuddyError
    from layerlog.surveillance.autorender import recover_missed_ends

    class Down(FakeClient):
        def printer_status(self, printer_id):
            raise BambuddyError("down")

    assert recover_missed_ends(Down(status(), {20: "completed"}), 1, raw_dirs(tmp_path, 20),
                               lambda *a: None) == []
    assert recover_missed_ends(Down(status(), {}), 1, tmp_path / "missing", lambda *a: None) == []
