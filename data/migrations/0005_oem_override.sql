-- 0005: per-server manual OEM override.
--
-- When a BMC misreports its manufacturer (white-box boards, OEM rebadges the
-- normalizer doesn't know yet), auto plugin routing can pick the wrong vendor
-- recipe. ``oem_override`` lets the operator pin the OEM in the UI; when set,
-- it wins over auto-detection for plugin routing (flash/provision/power) and
-- credential scoping. NULL/empty = auto (the default).
--
-- Deliberately NOT part of ServersRepo._FIELDS: discovery re-upserts rows
-- without this key, and including it would silently wipe the operator's
-- choice on every scan. Only ServersRepo.set_oem_override writes it.

ALTER TABLE servers ADD COLUMN oem_override TEXT;
