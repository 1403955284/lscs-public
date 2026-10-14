import pytest
from helpers import MINI_LOG, TEST_POOL

from lscs.pipeline import Pipeline
from lscs.replay import read_log_lines
from lscs.state.tracker import StateTracker


@pytest.fixture(autouse=True)
def _keep_cli_away_from_real_files(tmp_path_factory, monkeypatch):
    """命令行默认读项目目录下的 config.toml、把复盘写进 reviews/、把面板拖到的位置写进
    overlay-position.json、把本版本统计缓存在本机的缓存目录：测试里都换成临时目录（需要配置
    文件的测试自己再换成写好的临时文件）。测试不碰网络：urllib 的 urlopen 一调用就报错（要测
    默认 opener 的测试自己再换掉它）。"""
    import urllib.request

    import lscs.cli
    from lscs.overlay import position

    home = tmp_path_factory.mktemp("lscs-home")
    monkeypatch.setattr(lscs.cli, "DEFAULT_CONFIG", home / "config.toml")
    monkeypatch.setattr(lscs.cli, "REVIEW_BASE", home)
    monkeypatch.setattr(lscs.cli, "STATS_CACHE_DIR", home / "cache")
    monkeypatch.setattr(position, "DEFAULT_PATH", home / "overlay-position.json")

    def no_network(*_args, **_kwargs):
        raise OSError("测试不碰网络")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)


@pytest.fixture
def run_log():
    """把日志行全部推过 Pipeline，返回 [(state, events), ...]。"""

    def run(lines, pool=TEST_POOL):
        updates = []
        pipeline = Pipeline(StateTracker(pool), lambda s, e: updates.append((s, e)))
        for line in lines:
            pipeline.push(line)
        pipeline.flush()
        return updates

    return run


@pytest.fixture
def mini_lines() -> list[str]:
    return list(read_log_lines(MINI_LOG))
