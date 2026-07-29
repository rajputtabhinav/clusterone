"""Plugin registry: loads bundled plugins from ``plugins/*/manifest.json`` and
third-party plugins from the ``clusterone.plugins`` entry-point group, then
routes hosts to the highest-confidence plugin via ``match()``.
"""
from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path

from core.plugins_api.base import OEMPlugin, RedfishProbe

log = logging.getLogger(__name__)


class PluginRegistry:
    def __init__(self) -> None:
        self._plugins: list[OEMPlugin] = []

    def load_bundled(self, plugins_dir: Path) -> None:
        for manifest_path in sorted(plugins_dir.glob("*/manifest.json")):
            pkg = manifest_path.parent.name
            try:
                meta = json.loads(manifest_path.read_text(encoding="utf-8"))
                module = importlib.import_module(f"plugins.{pkg}.plugin")
                attr = meta.get("instance", "PLUGIN")
                instance = getattr(module, attr)
                self._plugins.append(instance)
                log.info("Loaded bundled plugin: %s", meta.get("key", pkg))
            except Exception as exc:
                log.exception("Failed loading plugin %s: %s", pkg, exc)

    def load_entry_points(self) -> None:
        try:
            from importlib.metadata import entry_points
        except ImportError:
            return
        try:
            eps = entry_points(group="clusterone.plugins")
        except TypeError:
            # older importlib.metadata API
            try:
                eps = entry_points().get("clusterone.plugins", [])
            except Exception:
                return
        for ep in eps:
            try:
                obj = ep.load()
                instance = obj() if callable(obj) else obj
                self._plugins.append(instance)
                log.info("Loaded entry-point plugin: %s", ep.name)
            except Exception as exc:
                log.exception("Failed loading entry-point plugin %s: %s", ep.name, exc)

    def match(self, probe: RedfishProbe) -> OEMPlugin | None:
        """Return the highest-confidence plugin for the probed host.

        Tie-breaking is deterministic: when two plugins return the same
        score, the one with a HIGHER ``priority`` (class attribute, default
        0) wins; if priorities also tie, the one with the lexically smaller
        ``key`` wins. Without this, manifest load order silently decided
        the winner on ties — a Tyrone-rebadged box scored equally by
        SupermicroPlugin and a hypothetical third-party plugin could route
        either way depending on which loaded first.
        """
        # Build (-score, -priority, key, plugin) tuples and pick the min.
        # Negated so ascending sort puts the best candidate first.
        candidates: list[tuple] = []
        for plugin in self._plugins:
            try:
                score = float(plugin.match(probe))
            except Exception:
                score = -1.0
            priority = int(getattr(plugin, "priority", 0))
            candidates.append((-score, -priority, getattr(plugin, "key", ""), plugin))
        if not candidates:
            return None
        candidates.sort()
        best_neg_score, _, _, best_plugin = candidates[0]
        if -best_neg_score < 0:
            return None
        # Log the runner-up for debug visibility on close calls
        if len(candidates) > 1:
            _, _, runner_key, runner = candidates[1]
            if candidates[0][0] == candidates[1][0]:
                log.info("Plugin match tie at score=%.2f — %s won over %s by priority/key",
                         -candidates[0][0], getattr(best_plugin, "key", "?"), runner_key)
        return best_plugin

    def resolve(self, probe: RedfishProbe,
                override_key: str | None = None) -> OEMPlugin | None:
        """MANUAL-ONLY routing (operator decision 2026-06-12).

        Returns the plugin the operator pinned for this server
        (``servers.oem_override``), or ``None`` when no OEM is selected or the
        pinned key has no installed plugin. Confidence-scored auto-matching is
        NOT used for operations anymore — every server must have an explicit
        OEM picked in the UI before flash/provision/power. ``match()`` remains
        only for scan-time identity reads (where no row exists yet).
        """
        if not override_key:
            return None
        plugin = self.by_key(override_key)
        if plugin is None:
            log.warning("OEM override %r has no loaded plugin", override_key)
        return plugin

    def all(self) -> list[OEMPlugin]:
        return list(self._plugins)

    def by_key(self, key: str) -> OEMPlugin | None:
        for p in self._plugins:
            if p.key == key:
                return p
        return None
