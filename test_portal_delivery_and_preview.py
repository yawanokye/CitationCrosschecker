"""The free/paid boundary and delivery diagnostics are enforced server-side."""
from contextlib import contextmanager
import logging
import smtplib
from unittest.mock import Mock

import pytest

import payment_notifications as mail
import subscription_portal as portal
from entitlements import apply_entitlements_to_result


def test_free_acii_hides_assessment_but_keeps_coverage_and_safety_alerts():
    source = {
        "summary": {"reference_entries_found": 4},
        "acii": {"ACII": 81, "score_available": True, "category": "Good",
                 "components": {"verification": {"score": 90}},
                 "recommendations": {"priority": "a confidential assessment"},
                 "coverage": {"publication_status_checked": 4, "expected_references": 4, "complete": True}},
        "source_risk_review": {"risks": [{"risk": "retracted_or_withdrawn", "reference": "safety warning"}]},
    }
    preview = apply_entitlements_to_result(source, paid=False)
    assert preview['acii']['locked'] is True
    assert preview['acii']['coverage']['complete'] is True
    for key in ('ACII', 'category', 'components', 'recommendations'):
        assert key not in preview['acii']
    assert preview['source_risk_review']['risks'][0]['reference'] == 'safety warning'
    paid = apply_entitlements_to_result(source, paid=True)
    assert paid['acii']['ACII'] == 81 and paid['acii']['components']['verification']['score'] == 90
    assert source['acii']['recommendations']['priority'] == 'a confidential assessment'


def test_blank_smtp_port_uses_verified_starttls(monkeypatch):
    for key, value in {'SMTP_HOST':'smtp.example.test','SMTP_PORT':'', 'SMTP_USERNAME':' mail-user ',
                       'SMTP_PASSWORD':'test-secret','SMTP_FROM_EMAIL':'sender@example.test'}.items():
        monkeypatch.setenv(key,value)
    server=Mock(); factory=Mock(); factory.return_value.__enter__=Mock(return_value=server)
    factory.return_value.__exit__=Mock(return_value=False)
    monkeypatch.setattr(mail.smtplib,'SMTP',factory)
    assert mail.probe_smtp_connection()
    assert factory.call_args.args[:2] == ('smtp.example.test',587)
    server.starttls.assert_called_once()
    server.login.assert_called_once_with('mail-user','test-secret')
    server.send_message.assert_not_called()


def test_failed_signin_logs_only_safe_exception_category(monkeypatch,caplog):
    monkeypatch.setenv('SMTP_FROM_EMAIL','sender@example.test')
    monkeypatch.setattr(portal,'smtp_ready',lambda:True)
    secret='do-not-log-provider-text-or-password'
    def fail(_message): raise smtplib.SMTPAuthenticationError(535,secret.encode())
    monkeypatch.setattr(portal,'_send',fail)
    with caplog.at_level(logging.WARNING), pytest.raises(portal.ledger.PortalError) as exc:
        portal.send_login_code('author@example.test','123456')
    assert exc.value.status == 503
    assert 'SMTPAuthenticationError smtp_code=535' in caplog.text
    assert secret not in caplog.text and '123456' not in caplog.text and 'author@example.test' not in caplog.text


def test_probe_does_not_send_any_message_and_sanitizes_failure(monkeypatch,caplog):
    monkeypatch.setattr(mail,'smtp_ready',lambda:True)
    @contextmanager
    def failed_connection():
        raise smtplib.SMTPAuthenticationError(535,b'private provider details')
        yield
    monkeypatch.setattr(mail,'_smtp_connection',failed_connection)
    with caplog.at_level(logging.WARNING): assert not mail.probe_smtp_connection()
    assert 'SMTPAuthenticationError smtp_code=535' in caplog.text
    assert 'private provider details' not in caplog.text
