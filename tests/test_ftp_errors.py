"""How the FTP client and the run behave when the day of today has no folder yet (JAXA answers 450, not 550)."""
import datetime as dt
import ftplib

import pytest

import gsmap
from test_summary import TODAY, Server, regions_file, SECRET_USER, SECRET_PW


REAL_FTP = gsmap.Ftp                                 # the tests replace gsmap.Ftp, keep the real class


class FakeFtp:
    def __init__(self, error):
        self.error, self.calls = error, 0

    def nlst(self, path):
        self.calls += 1
        raise self.error


def client(error):
    c = REAL_FTP("h", "u", "p", ["/base"])
    c.fake = FakeFtp(error)
    c.ftp = c.fake
    c._connect = lambda: setattr(c, "ftp", c.fake)  # a reconnect gives the same fake back
    return c


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(gsmap.time, "sleep", lambda s: None)


@pytest.mark.parametrize("message", ["550 /base/2026/11/12: No such file or directory",
                                     "450 /base/2026/11/12: No such file or directory"])
def test_a_folder_that_does_not_exist_yet_is_an_empty_listing(message):
    err = ftplib.error_perm(message) if message.startswith("550") else ftplib.error_temp(message)
    c = client(err)
    assert c.list_dir("/base/2026/11/12") == []
    assert c.day_files(dt.date(2026, 11, 12)) == {}
    assert c.fake.calls >= 1


def test_other_temporary_errors_are_still_retried_and_raised():
    c = client(ftplib.error_temp("421 Service not available"))
    with pytest.raises(ftplib.error_temp):
        c.list_dir("/base/2026/11/12")
    assert c.fake.calls == 3                         # tried three times, not swallowed


class MissingTodayFolder(Server):
    """Like the real server at 02:00 UTC: today's folder is not there and the listing answers 450."""

    def day_files(self, day):
        if day == TODAY:
            raise ftplib.error_temp(f"450 /realtime_ver/v8/hourly/{day:%Y/%m/%d}: No such file or directory")
        return super().day_files(day)


class BrokenToday(Server):
    def day_files(self, day):
        if day == TODAY:
            raise ftplib.error_temp("421 Service not available")
        return super().day_files(day)


def run(monkeypatch, tmp_path, server, capsys=None):
    monkeypatch.setenv("GSMAP_USER", SECRET_USER)
    monkeypatch.setenv("GSMAP_PASSWORD", SECRET_PW)
    monkeypatch.setattr(gsmap, "Ftp", server)
    assets = regions_file(tmp_path / "assets")
    gsmap.main(["--end", "2026-11-09", "--today", str(TODAY), "--include-today", "--out", str(tmp_path / "site"),
                "--cache-dir", str(tmp_path / "cache"), "--assets", str(assets), "--workers", "1",
                "--publish-dir", str(tmp_path / "pub")])
    import json
    return json.loads((tmp_path / "pub" / "summary.json").read_text())


def test_a_run_before_the_first_file_of_the_day_still_publishes(monkeypatch, tmp_path):
    """The real server without today's folder: 450 is an empty day, so today is listed with 0 hours."""
    class Real(Server):
        def __call__(self, *a, **k):
            c = client(ftplib.error_temp("450 /base/x: No such file or directory"))
            c.day_files = lambda day, orig=c.day_files: Server.day_files(self, day) if day != TODAY else orig(day)
            c.download = lambda path: Server.download(self, path)
            c.close = lambda: None
            return c
    s = run(monkeypatch, tmp_path, Real())
    assert s["days"][-1] == {"date": "2026-11-10", "hours": 0, "partial": True}
    assert [d["hours"] for d in s["days"][:-1]] == [24] * 7


def test_a_failure_for_today_never_takes_the_page_down(monkeypatch, tmp_path, capsys):
    """--include-today only feeds summary.json: if today cannot be read, the week is still built without it."""
    s = run(monkeypatch, tmp_path, BrokenToday())
    assert [d["date"] for d in s["days"]][-1] == "2026-11-09" and len(s["days"]) == 7
    assert (tmp_path / "site" / "data" / "manifest.json").exists()
    assert "2026-11-10" in capsys.readouterr().err                      # a warning names the day
