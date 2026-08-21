"""Unit tests for bridge error classification and reCAPTCHA self-healing detection."""

from flow_engine.bridge import ExtensionBridge


def test_is_unauthenticated():
    assert ExtensionBridge._is_unauthenticated({"status": 401}) is True
    assert ExtensionBridge._is_unauthenticated({"data": {"error": {"code": 401}}}) is True
    assert ExtensionBridge._is_unauthenticated({"data": {"error": {"status": "UNAUTHENTICATED"}}}) is True
    assert ExtensionBridge._is_unauthenticated({"status": 200}) is False
    assert ExtensionBridge._is_unauthenticated({"status": 400}) is False


def test_is_recaptcha_failure():
    # 400 with UNUSUAL_ACTIVITY
    err_400 = {
        "status": 400,
        "data": {
            "error": {
                "code": 400,
                "message": "reCAPTCHA evaluation failed (PUBLIC_ERROR_UNUSUAL_ACTIVITY)",
                "status": "INVALID_ARGUMENT",
            }
        }
    }
    assert ExtensionBridge._is_recaptcha_failure(err_400) is True

    # 403 / 400 string error
    err_str = {
        "status": 400,
        "data": "Generation failed: reCAPTCHA verification error"
    }
    assert ExtensionBridge._is_recaptcha_failure(err_str) is True

    # Extension direct captcha failed error
    err_ext = {
        "status": 403,
        "error": "CAPTCHA_FAILED: timeout"
    }
    assert ExtensionBridge._is_recaptcha_failure(err_ext) is True

    # Regular 400 (not captcha)
    err_other = {
        "status": 400,
        "data": {"error": {"message": "Invalid aspect ratio"}}
    }
    assert ExtensionBridge._is_recaptcha_failure(err_other) is False

    # Success
    assert ExtensionBridge._is_recaptcha_failure({"status": 200, "data": {}}) is False
