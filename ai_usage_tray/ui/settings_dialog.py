from __future__ import annotations

import threading
from typing import Callable, Optional

from PyQt6.QtCore import pyqtSignal, QObject
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ai_usage_tray.core.config import ConfigManager
from ai_usage_tray.core.provider import ProviderRegistry
from ai_usage_tray.providers.openai_codex import find_codex_binary


class _OAuthSignals(QObject):
    done = pyqtSignal(str, bool, str)


class SettingsDialog(QDialog):
    """Provider configuration, polling intervals, and OAuth account management."""

    def __init__(
        self,
        config_manager: ConfigManager,
        registry: ProviderRegistry,
        on_settings_saved: Callable[[], None],
        latest_snapshots: Optional[dict] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("AIUsageTray Settings")
        self.setMinimumWidth(560)
        self.config_manager = config_manager
        self.registry = registry
        self.on_settings_saved = on_settings_saved
        self.latest_snapshots = latest_snapshots or {}

        self._oauth_signals = _OAuthSignals(self)
        self._oauth_signals.done.connect(self._on_oauth_finished)

        self._provider_controls: dict[str, dict[str, object]] = {}
        self._build_ui()

    def _populate_tray_bucket_combo(self, provider_id: str) -> None:
        self.tray_bucket_combo.clear()
        self.tray_bucket_combo.addItem("Lowest in Provider", "")
        if provider_id == "auto":
            self.tray_bucket_combo.setEnabled(False)
            return
        self.tray_bucket_combo.setEnabled(True)
        snap = self.latest_snapshots.get(provider_id)
        if snap is not None:
            for b in snap.all_buckets:
                self.tray_bucket_combo.addItem(f"{b.display_name} ({b.bucket_id})", b.bucket_id)
        saved_bucket = self.config_manager.config.tray_bucket_id
        idx = self.tray_bucket_combo.findData(saved_bucket)
        if idx >= 0:
            self.tray_bucket_combo.setCurrentIndex(idx)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)

        mode_choices = {
            "antigravity": [
                ("Auto (Local LS → OAuth)", "auto"),
                ("Local Language Server Only", "local_only"),
                ("Google OAuth Only", "oauth_only"),
            ],
            "openai_codex": [
                ("Auto (Codex Binary → OAuth)", "auto"),
                ("Codex Binary Only", "codex_binary"),
                ("OpenAI OAuth Only", "oauth"),
            ],
            "claude": [
                ("Auto (OAuth / CLI → Desktop)", "auto"),
                ("OAuth / Claude Code Only", "oauth_only"),
                ("Claude Desktop Only", "desktop_only"),
            ],
            "notebooklm": [
                ("Browser Session Cookies (Auto)", "auto"),
            ],
            "gemini_web": [
                ("Browser Session Cookies (Auto)", "auto"),
            ],
            "github_copilot": [
                ("Auto (OAuth / Local / gh CLI)", "auto"),
            ],
        }

        from PyQt6.QtWidgets import QScrollArea

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_body = QWidget()
        scroll_layout = QVBoxLayout(scroll_body)
        scroll_layout.setSpacing(10)
        scroll.setWidget(scroll_body)
        root.addWidget(scroll, 1)
        self.setMinimumHeight(620)

        tray_box = QGroupBox("Tray Icon Display")
        tray_form = QFormLayout(tray_box)
        self.tray_provider_combo = QComboBox()
        self.tray_provider_combo.addItem("Lowest Across All Providers", "auto")
        for provider in self.registry.all():
            self.tray_provider_combo.addItem(provider.display_name, provider.provider_id)
        p_idx = self.tray_provider_combo.findData(self.config_manager.config.tray_provider)
        if p_idx >= 0:
            self.tray_provider_combo.setCurrentIndex(p_idx)
        self.tray_bucket_combo = QComboBox()
        self.tray_provider_combo.currentIndexChanged.connect(
            lambda: self._populate_tray_bucket_combo(str(self.tray_provider_combo.currentData()))
        )
        self._populate_tray_bucket_combo(str(self.tray_provider_combo.currentData()))
        tray_form.addRow("Provider:", self.tray_provider_combo)
        tray_form.addRow("Metric:", self.tray_bucket_combo)
        scroll_layout.addWidget(tray_box)

        for provider in self.registry.all():
            pid = provider.provider_id
            pcfg = self.config_manager.config.get_provider(pid)
            box = QGroupBox(provider.display_name)
            form = QFormLayout(box)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
            form.setVerticalSpacing(6)

            enabled_cb = QCheckBox("Enable provider")
            enabled_cb.setChecked(pcfg.enabled)
            form.addRow(enabled_cb)

            mode_combo = QComboBox()
            for label, val in mode_choices.get(pid, [("Auto", "auto")]):
                mode_combo.addItem(label, val)
            idx = mode_combo.findData(pcfg.mode)
            if idx >= 0:
                mode_combo.setCurrentIndex(idx)
            form.addRow("Source Mode:", mode_combo)

            poll_spin = QSpinBox()
            poll_spin.setRange(15, 3600)
            poll_spin.setSuffix(" s")
            poll_spin.setValue(pcfg.poll_interval_s)
            form.addRow("Poll Interval:", poll_spin)

            controls: dict[str, object] = {
                "enabled": enabled_cb,
                "mode": mode_combo,
                "poll": poll_spin,
            }

            if pid == "openai_codex":
                bin_row = QHBoxLayout()
                bin_edit = QLineEdit(pcfg.binary_path)
                bin_edit.setPlaceholderText("Auto-detect codex binary")
                detect_btn = QPushButton("Detect")
                detect_btn.clicked.connect(lambda _=False, e=bin_edit: self._detect_codex(e))
                bin_row.addWidget(bin_edit, 1)
                bin_row.addWidget(detect_btn)
                form.addRow("Codex Binary:", bin_row)
                controls["binary_path"] = bin_edit

            if provider.supports_oauth:
                client_id_edit = QLineEdit(pcfg.oauth_client_id)
                client_id_edit.setPlaceholderText("Default / Optional custom OAuth Client ID")
                form.addRow("OAuth Client ID:", client_id_edit)
                controls["client_id"] = client_id_edit

                if pid == "antigravity":
                    secret_edit = QLineEdit(pcfg.oauth_client_secret)
                    secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
                    secret_edit.setPlaceholderText("Optional Google OAuth Client Secret")
                    form.addRow("Client Secret:", secret_edit)
                    controls["client_secret"] = secret_edit

            auth_status = provider.get_auth_status()
            status_lbl = QLabel(auth_status.source_description)
            status_lbl.setWordWrap(True)
            status_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.MinimumExpanding)
            status_lbl.setMinimumHeight(20)
            form.addRow("Status:", status_lbl)
            controls["status_label"] = status_lbl

            if provider.supports_oauth:
                oauth_row = QHBoxLayout()
                connect_btn = QPushButton("Connect OAuth")
                connect_btn.clicked.connect(lambda _=False, p_id=pid: self._start_oauth(p_id))
                disconnect_btn = QPushButton("Disconnect OAuth")
                disconnect_btn.clicked.connect(lambda _=False, p_id=pid: self._disconnect_oauth(p_id))
                oauth_row.addWidget(connect_btn)
                oauth_row.addWidget(disconnect_btn)
                form.addRow("Account:", oauth_row)

            self._provider_controls[pid] = controls
            scroll_layout.addWidget(box)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        save_btn = QPushButton("Save & Apply")
        save_btn.clicked.connect(self._on_save)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(save_btn)
        btn_row.addWidget(cancel_btn)
        root.addLayout(btn_row)

    def _detect_codex(self, line_edit: QLineEdit) -> None:
        found = find_codex_binary("", allow_slow_search=True)
        if found:
            line_edit.setText(found)
        else:
            QMessageBox.information(self, "Codex Binary", "Could not find a 'codex' executable on PATH or standard paths.")

    def _apply_controls_to_config(self) -> None:
        self.config_manager.config.tray_provider = str(self.tray_provider_combo.currentData() or "auto")
        self.config_manager.config.tray_bucket_id = str(self.tray_bucket_combo.currentData() or "")
        for pid, ctrl in self._provider_controls.items():
            pcfg = self.config_manager.config.get_provider(pid)
            pcfg.enabled = ctrl["enabled"].isChecked()  # type: ignore[attr-defined]
            pcfg.mode = str(ctrl["mode"].currentData())  # type: ignore[attr-defined]
            pcfg.poll_interval_s = int(ctrl["poll"].value())  # type: ignore[attr-defined]
            if "client_id" in ctrl:
                pcfg.oauth_client_id = str(ctrl["client_id"].text()).strip()  # type: ignore[attr-defined]
            if "binary_path" in ctrl:
                pcfg.binary_path = str(ctrl["binary_path"].text()).strip()  # type: ignore[attr-defined]
            if "client_secret" in ctrl:
                pcfg.oauth_client_secret = str(ctrl["client_secret"].text()).strip()  # type: ignore[attr-defined]
        self.config_manager.save()

    def _start_oauth(self, provider_id: str) -> None:
        self._apply_controls_to_config()
        provider = self.registry.get(provider_id)
        if provider is None:
            return
        status_lbl = self._provider_controls[provider_id]["status_label"]
        status_lbl.setText("Waiting for browser OAuth login...")  # type: ignore[attr-defined]

        def _worker() -> None:
            ok, msg = provider.start_oauth_login()
            self._oauth_signals.done.emit(provider_id, ok, msg)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_oauth_finished(self, provider_id: str, ok: bool, msg: str) -> None:
        provider = self.registry.get(provider_id)
        if provider and provider_id in self._provider_controls:
            status = provider.get_auth_status()
            self._provider_controls[provider_id]["status_label"].setText(status.source_description)  # type: ignore[attr-defined]
        if ok:
            QMessageBox.information(self, "OAuth Connected", msg)
            self.on_settings_saved()
        else:
            QMessageBox.warning(self, "OAuth Failed", msg)

    def _disconnect_oauth(self, provider_id: str) -> None:
        provider = self.registry.get(provider_id)
        if provider is None:
            return
        provider.disconnect_oauth()
        status = provider.get_auth_status()
        self._provider_controls[provider_id]["status_label"].setText(status.source_description)  # type: ignore[attr-defined]
        self.on_settings_saved()

    def _on_save(self) -> None:
        self._apply_controls_to_config()
        self.on_settings_saved()
        self.accept()
