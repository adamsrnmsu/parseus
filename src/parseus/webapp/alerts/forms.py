import json

from django import forms

from .models import SysmonRule


class SysmonRuleForm(forms.ModelForm):
    """Form for creating and editing UI-managed Sysmon analytic rules."""

    allowed_windows_json = forms.CharField(
        label="Allowed windows",
        required=False,
        widget=forms.Textarea(attrs={"rows": 5, "class": "form-control font-monospace"}),
        help_text=(
            "JSON list. Example: "
            '[{"days":["mon","thu","fri"],"start":"09:00","end":"17:00"}]. '
            "Leave blank or [] to allow this value at any time."
        ),
    )

    class Meta:
        model = SysmonRule
        fields = [
            "name",
            "analytic_type",
            "match_value",
            "enabled",
            "timezone",
            "description",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "analytic_type": forms.Select(attrs={"class": "form-select"}),
            "match_value": forms.TextInput(attrs={"class": "form-control"}),
            "enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "timezone": forms.TextInput(attrs={"class": "form-control"}),
            "description": forms.Textarea(attrs={"rows": 3, "class": "form-control"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["allowed_windows_json"].initial = json.dumps(
                self.instance.allowed_windows,
                indent=2,
            )
        else:
            self.fields["allowed_windows_json"].initial = json.dumps(
                [
                    {
                        "days": ["mon", "thu", "fri"],
                        "start": "09:00",
                        "end": "17:00",
                    }
                ],
                indent=2,
            )

    def clean_allowed_windows_json(self):
        value = self.cleaned_data.get("allowed_windows_json", "").strip()
        if not value:
            return []
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as err:
            raise forms.ValidationError(f"Invalid JSON: {err}") from err
        if not isinstance(parsed, list):
            raise forms.ValidationError("Allowed windows must be a JSON list.")
        return parsed

    def clean(self):
        cleaned_data = super().clean()
        if "allowed_windows_json" in cleaned_data:
            # Model validation runs afterwards in ModelForm._post_clean.
            self.instance.allowed_windows = cleaned_data["allowed_windows_json"]
        return cleaned_data

    def _update_errors(self, errors):
        """Report model ``allowed_windows`` errors on the form's JSON field."""
        error_dict = getattr(errors, "error_dict", None)
        if error_dict and "allowed_windows" in error_dict:
            error_dict["allowed_windows_json"] = error_dict.pop("allowed_windows")
        super()._update_errors(errors)

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.allowed_windows = self.cleaned_data["allowed_windows_json"]
        if commit:
            instance.save()
        return instance
