"""Characterization tests for SysmonRule.clean and SysmonRuleForm validation."""

import json

import pytest
from django.core.exceptions import ValidationError

from parseus.webapp.alerts.forms import SysmonRuleForm
from parseus.webapp.alerts.models import SysmonRule

GOOD_WINDOW = {"days": ["mon", "Thu"], "start": "09:00", "end": "17:00:00"}


def _rule(**kw):
    kw.setdefault("timezone", "UTC")
    kw.setdefault("allowed_windows", [])
    return SysmonRule(
        name="r", analytic_type="user", match_value="bob", **kw
    )


def _errors(rule):
    with pytest.raises(ValidationError) as exc:
        rule.clean()
    return exc.value.message_dict


def test_clean_accepts_valid_rule_and_empty_windows():
    _rule().clean()
    _rule(allowed_windows=[GOOD_WINDOW, {"start": "00:00", "end": "01:00"}]).clean()


def test_clean_rejects_unknown_timezone():
    assert _errors(_rule(timezone="Mars/Base")) == {"timezone": ["Unknown timezone."]}


def test_clean_rejects_non_list_windows():
    assert _errors(_rule(allowed_windows={"a": 1})) == {
        "allowed_windows": ["Allowed windows must be a JSON list."]
    }


@pytest.mark.parametrize(
    "windows,message",
    [
        (["nope"], "Window 1 must be an object."),
        ([GOOD_WINDOW, {"days": "mon", "start": "1:00", "end": "2:00"}],
         "Window 2 days must be a list."),
        ([{"days": ["mon", "xyz"], "start": "09:00", "end": "10:00"}],
         "Window 1 has invalid day(s): xyz. Use mon, tue, wed, thu, fri, sat, sun."),
        ([{"days": [5], "start": "09:00", "end": "10:00"}],
         "Window 1 has invalid day(s): 5. Use mon, tue, wed, thu, fri, sat, sun."),
        ([{"days": ["mon"], "end": "10:00"}], "Window 1 missing start."),
        ([{"days": ["mon"], "start": "09:00"}], "Window 1 missing end."),
        ([{"start": "9am", "end": "10:00"}],
         "Window 1 start must use HH:MM or HH:MM:SS format."),
        ([{"start": "09:00", "end": "late"}],
         "Window 1 end must use HH:MM or HH:MM:SS format."),
    ],
)
def test_clean_rejects_bad_windows(windows, message):
    assert _errors(_rule(allowed_windows=windows)) == {"allowed_windows": [message]}


def _form(**overrides):
    data = {
        "name": "r",
        "analytic_type": "user",
        "match_value": "bob",
        "timezone": "UTC",
        "enabled": "on",
        "description": "d",
        "allowed_windows_json": "",
    }
    data.update(overrides)
    return SysmonRuleForm(data=data)


def test_form_blank_json_means_always_allowed():
    form = _form()
    assert form.is_valid()
    assert form.cleaned_data["allowed_windows_json"] == []
    assert form.instance.allowed_windows == []
    assert form.instance.match_value == "bob"
    assert form.instance.enabled is True


def test_form_valid_json_windows_flow_to_instance():
    form = _form(allowed_windows_json='[{"days":["mon"],"start":"09:00","end":"17:00"}]')
    assert form.is_valid()
    assert form.instance.allowed_windows == [
        {"days": ["mon"], "start": "09:00", "end": "17:00"}
    ]


def test_form_rejects_invalid_json():
    form = _form(allowed_windows_json="{oops")
    assert not form.is_valid()
    assert form.errors["allowed_windows_json"][0].startswith("Invalid JSON:")


def test_form_rejects_non_list_json():
    form = _form(allowed_windows_json='{"a": 1}')
    assert not form.is_valid()
    assert form.errors["allowed_windows_json"] == ["Allowed windows must be a JSON list."]


def test_form_maps_model_window_errors_to_json_field():
    form = _form(allowed_windows_json='[{"start":"bad","end":"10:00"}]')
    assert not form.is_valid()
    assert form.errors["allowed_windows_json"] == [
        "Window 1 start must use HH:MM or HH:MM:SS format."
    ]


def test_form_maps_model_timezone_error_to_timezone_field():
    form = _form(timezone="Mars/Base")
    assert not form.is_valid()
    assert form.errors["timezone"] == ["Unknown timezone."]


def test_form_skips_model_validation_when_required_field_missing():
    form = _form(name="")
    assert not form.is_valid()
    assert list(form.errors) == ["name"]


def test_form_initial_windows_default_and_from_instance():
    default = SysmonRuleForm().fields["allowed_windows_json"].initial
    assert '"mon"' in default and '"09:00"' in default
    rule = _rule(allowed_windows=[GOOD_WINDOW])
    rule.pk = 5
    initial = SysmonRuleForm(instance=rule).fields["allowed_windows_json"].initial


    assert json.loads(initial) == [GOOD_WINDOW]


@pytest.mark.django_db
def test_form_save_persists_parsed_windows():
    form = _form(allowed_windows_json='[{"days":["fri"],"start":"01:00","end":"02:00"}]')
    assert form.is_valid()
    saved = form.save()
    saved.refresh_from_db()
    assert saved.allowed_windows == [{"days": ["fri"], "start": "01:00", "end": "02:00"}]


@pytest.mark.django_db
def test_form_save_without_commit_does_not_persist():
    form = _form(allowed_windows_json='[{"start":"01:00","end":"02:00"}]')
    assert form.is_valid()
    unsaved = form.save(commit=False)
    assert unsaved.pk is None and SysmonRule.objects.count() == 0
    assert unsaved.allowed_windows == [{"start": "01:00", "end": "02:00"}]
