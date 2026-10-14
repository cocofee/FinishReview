from dataclasses import asdict
import time

import pytest
from PyQt5.QtWidgets import QApplication

from realtime.finish_line import FinishLine
from realtime.finish_problem_panel import FinishProblemDialog
from realtime.finish_problems import chip_basis, digest, scoped_chips
from tests.realtime.test_finish_problems import inputs
from tests.realtime.test_offline_finish import make_archive


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def test_worker_round_trip_decision_and_stale_report_block(qapp, tmp_path):
    archive = tmp_path / "archive.avi"
    make_archive(archive)
    request, _ = inputs()
    request.update(output_dir=str(tmp_path), finish_line=asdict(FinishLine(1, .2, .5, .8, .5)))
    request["sources"] = [dict(segment_id="video", video_path=str(archive), origin_ms=200,
                                camera_index=1, race_id="race")]
    signature = [str(tmp_path), request["scope"],
                 digest([chip_basis(c) for c in scoped_chips(request["chips"], request["scope"])]),
                 request["input_signature"]]
    def factory(lap, line):
        return dict(request, final_lap=lap, finish_line=line or request["finish_line"])
    dialog = FinishProblemDialog(factory, lambda: tuple(signature))
    dialog.lap.setValue(1)
    dialog._start()
    deadline = time.monotonic() + 30
    while dialog.report is None and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(.01)
    assert dialog.report is not None, dialog.hint.text()
    assert not dialog._busy()
    assert dialog.table.rowCount() == 1
    assert not dialog.canvas.image.isNull()
    assert dialog.locate_button.isEnabled()
    located = []
    dialog.locate_requested.connect(located.append)
    dialog._locate()
    assert located[0]["locator"]["frame_index"] == 1740
    dialog.reason.setText("pedestrian confirmed by operator")
    dialog.action.setCurrentIndex(dialog.action.findData("non_rider"))
    dialog._save()
    assert dialog.table.rowCount() == 0
    dialog.show_closed.setChecked(True)
    assert dialog.table.rowCount() == 1
    signature[3] = "changed recording"
    dialog._update_enabled()
    assert not dialog.save_button.isEnabled()
    assert not dialog.export_button.isEnabled()
    assert not dialog.locate_button.isEnabled()
    dialog.close()


def test_empty_archives_are_explained_without_launch(qapp, tmp_path):
    request, _ = inputs()
    request.update(output_dir=str(tmp_path), sources=[], finish_line=None)
    dialog = FinishProblemDialog(lambda *_args: request, lambda: None)
    assert not dialog.start_button.isEnabled()
    assert "录像" in dialog.hint.text()
    assert not dialog._busy()
    dialog.close()
