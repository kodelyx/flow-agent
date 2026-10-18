"""Make the package importable from the test suite.

Inserts the project root into sys.path so tests can import `flow_agent` and `main` cleanly.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

@pytest.fixture(autouse=True)
def isolate_api_keys_for_tests(tmp_path, monkeypatch):
    """
    모든 테스트에 대해 API 키 저장소를 임시 경로로 격리합니다.

    실제 운영 환경의 data/api_keys.json 파일이나 시스템 환경 변수(FLOW_API_KEY)가
    테스트 스위트에 유입되어 인증 오류(401)를 발생시키는 것을 방지합니다.
    """
    test_key_file = tmp_path / "isolated_api_keys.json"
    monkeypatch.setattr("flow_agent.auth.DEFAULT_KEY_FILE", test_key_file)
    monkeypatch.delenv("FLOW_API_KEY", raising=False)
    yield test_key_file
