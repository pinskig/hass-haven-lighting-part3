"""Registry migration policy with isolated in-memory registry contract doubles.

These tests exercise identity decisions, not Home Assistant framework behavior.
The integration's separate real-HA tests cover the supported registry APIs.
"""

from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


INTEGRATION = Path(__file__).resolve().parents[1] / "custom_components" / "haven"


def owned_by(device, entry_id):
    if hasattr(device, "config_entry_id"):
        return device.config_entry_id == entry_id
    return entry_id in device.config_entries


class DeviceRegistry:
    def __init__(self):
        self.devices = {}
        self.calls = []

    def add(self, device_id, identifiers, *, owners=("entry-one",), **kwargs):
        data = dict(
            id=device_id,
            identifiers=set(identifiers),
            config_entries=set(owners),
            model="Haven Zone",
            name="Cloud name",
            name_by_user="My patio device",
            area_id="garden",
            via_device_id=None,
        )
        data.update(kwargs)
        device = SimpleNamespace(**data)
        self.devices[device_id] = device
        return device

    def async_get(self, device_id):
        return self.devices.get(device_id)

    def async_get_device(self, identifiers=None, connections=None):
        for device in self.devices.values():
            if device.identifiers & (identifiers or set()):
                return device
        return None

    def async_update_device(self, device_id, **changes):
        self.calls.append(("update", device_id, deepcopy(changes)))
        data = vars(self.devices[device_id]).copy()
        if "new_identifiers" in changes:
            data["identifiers"] = set(changes.pop("new_identifiers"))
        data.update(changes)
        result = SimpleNamespace(**data)
        self.devices[device_id] = result
        return result

    def async_get_or_create(self, *, config_entry_id, identifiers, **kwargs):
        self.calls.append(("get_or_create", None, deepcopy(dict(
            config_entry_id=config_entry_id, identifiers=identifiers, **kwargs
        ))))
        found = self.async_get_device(identifiers)
        if found is not None:
            # Older HA can share devices across entries. The migration must
            # preflight ownership instead of reaching this implicit attachment.
            found.config_entries.add(config_entry_id)
            return found
        device_id = f"created-{len(self.devices)}"
        return self.add(device_id, identifiers, owners=(config_entry_id,), **kwargs)


class EntityRegistry:
    def __init__(self):
        self.entities = {}
        self.calls = []

    def add(self, entity_id, unique_id, device_id, *, entry_id="entry-one"):
        entity = SimpleNamespace(
            entity_id=entity_id,
            unique_id=unique_id,
            device_id=device_id,
            config_entry_id=entry_id,
            domain="light",
            platform="haven",
            name="My patio light",
            area_id="terrace",
            disabled_by=None,
        )
        self.entities[entity_id] = entity
        return entity

    def async_get_entity_id(self, domain, platform, unique_id):
        return next((entity.entity_id for entity in self.entities.values()
                     if entity.domain == domain and entity.platform == platform
                     and entity.unique_id == unique_id), None)

    def async_get(self, entity_id):
        return self.entities.get(entity_id)

    def async_update_entity(self, entity_id, **changes):
        self.calls.append((entity_id, deepcopy(changes)))
        data = vars(self.entities[entity_id]).copy()
        if "new_unique_id" in changes:
            data["unique_id"] = changes.pop("new_unique_id")
        data.update(changes)
        result = SimpleNamespace(**data)
        self.entities[entity_id] = result
        return result


def load_registry_contract():
    """Import only registry.py and immutable models; restore all HA modules."""
    namespace = "_haven_registry_contract"
    fake_dr = ModuleType("homeassistant.helpers.device_registry")
    fake_dr.async_get = lambda hass: hass.device_registry
    fake_dr.async_entries_for_config_entry = lambda registry, entry_id: [
        device for device in registry.devices.values() if owned_by(device, entry_id)
    ]
    fake_er = ModuleType("homeassistant.helpers.entity_registry")
    fake_er.async_get = lambda hass: hass.entity_registry
    fake_er.async_entries_for_config_entry = lambda registry, entry_id: [
        entity for entity in registry.entities.values() if entity.config_entry_id == entry_id
    ]
    modules = {}
    for name in [namespace, f"{namespace}.havenlighting", f"{namespace}.const",
                 f"{namespace}.coordinator", "homeassistant", "homeassistant.config_entries",
                 "homeassistant.core", "homeassistant.exceptions", "homeassistant.helpers"]:
        modules[name] = ModuleType(name)
    modules[namespace].__path__ = [str(INTEGRATION)]
    modules[f"{namespace}.havenlighting"].__path__ = [str(INTEGRATION / "havenlighting")]
    modules[f"{namespace}.const"].DOMAIN = "haven"
    modules[f"{namespace}.coordinator"].HavenCoordinator = object
    modules["homeassistant.config_entries"].ConfigEntry = object
    modules["homeassistant.core"].HomeAssistant = object
    modules["homeassistant.core"].callback = lambda function: function
    modules["homeassistant.exceptions"].ConfigEntryError = type("ConfigEntryError", (Exception,), {})
    modules["homeassistant.helpers"].device_registry = fake_dr
    modules["homeassistant.helpers"].entity_registry = fake_er
    modules[fake_dr.__name__] = fake_dr
    modules[fake_er.__name__] = fake_er
    with patch.dict(sys.modules, modules):
        loaded = []
        for module_name, source in [
            (f"{namespace}.havenlighting.models", INTEGRATION / "havenlighting" / "models.py"),
            (f"{namespace}.registry", INTEGRATION / "registry.py"),
        ]:
            spec = importlib.util.spec_from_file_location(module_name, source)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            loaded.append(module)
    return loaded[0], loaded[1]


class RegistryMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.models, cls.subject = load_registry_contract()

    def setUp(self):
        self.devices = DeviceRegistry()
        self.entities = EntityRegistry()
        self.hass = SimpleNamespace(device_registry=self.devices, entity_registry=self.entities)
        self.entry = SimpleNamespace(entry_id="entry-one")
        self.warned = set()
        self.key = self.models.TargetKey(123456, "Zone", 101)
        self.coordinator = SimpleNamespace(
            account_id="42",
            location=SimpleNamespace(location_id=123456, name="Home"),
            data={self.key: self.models.LightState(self.key, "Patio", True, 128, 11)},
        )
        self.devices.add(
            "legacy-controller", {("haven", "123456")},
            model="Haven Controller/Location", name_by_user="My controller", area_id="utility",
        )
        self.devices.add(
            "legacy-target", {("haven", "101")}, via_device_id="legacy-controller"
        )
        self.entities.add("light.my_patio", "haven_light_101", "legacy-target")

    def prepare(self):
        self.subject.async_prepare_registries(
            self.hass, self.entry, [self.coordinator], self.warned
        )

    def assert_legacy_preserved(self, *, original_device=None):
        entity = self.entities.entities["light.my_patio"]
        self.assertEqual(entity.unique_id, "haven_light_101")
        self.assertEqual(entity.device_id, "legacy-target")
        self.assertEqual(entity.name, "My patio light")
        self.assertEqual(entity.area_id, "terrace")
        if original_device is not None:
            self.assertEqual(vars(self.devices.devices["legacy-target"]), original_device)

    def test_unambiguous_migration_preserves_entity_and_device_customization(self):
        self.prepare()
        entity = self.entities.entities["light.my_patio"]
        self.assertEqual(entity.unique_id, "haven:42:123456:zone:101")
        self.assertEqual(entity.device_id, "legacy-target")
        self.assertEqual(entity.name, "My patio light")
        self.assertEqual(entity.area_id, "terrace")
        target = self.devices.devices["legacy-target"]
        self.assertEqual(target.identifiers, {("haven", "haven:42:123456:zone:101")})
        self.assertEqual(target.name_by_user, "My patio device")
        self.assertEqual(target.area_id, "garden")
        self.assertEqual(target.via_device_id, "legacy-controller")
        controller = self.devices.devices["legacy-controller"]
        self.assertEqual(controller.identifiers, {("haven", "haven:42:location:123456")})
        self.assertEqual(controller.name_by_user, "My controller")
        self.assertEqual(controller.area_id, "utility")
        self.assertEqual(len(self.devices.devices), 2)

    def test_same_number_in_zone_and_group_uses_the_recorded_kind(self):
        group_key = self.models.TargetKey(123456, "Group", 101)
        self.coordinator.data[group_key] = self.models.LightState(group_key, "All", False, 255)
        self.prepare()
        self.assertEqual(self.entities.entities["light.my_patio"].unique_id, "haven:42:123456:zone:101")
        self.assertEqual(self.entities.entities["light.my_patio"].device_id, "legacy-target")
        grouped = self.devices.async_get_device({("haven", "haven:42:123456:group:101")})
        self.assertIsNotNone(grouped)
        self.assertNotEqual(grouped.id, "legacy-target")

    def test_missing_known_parent_is_not_replaced_by_a_live_other_location(self):
        for old_parent_identifier in ["999999", "haven:42:location:999999"]:
            with self.subTest(identifier=old_parent_identifier):
                self.setUp()
                self.devices.devices["legacy-controller"].identifiers = {("haven", old_parent_identifier)}
                original = deepcopy(vars(self.devices.devices["legacy-target"]))
                self.prepare()
                self.assert_legacy_preserved(original_device=original)

    def test_unknown_kind_or_parent_does_not_guess_from_a_single_live_target(self):
        cases = [("model", "Haven Device"), ("via_device_id", None),
                 ("via_device_id", "missing-controller")]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.setUp()
                setattr(self.devices.devices["legacy-target"], field, value)
                original = deepcopy(vars(self.devices.devices["legacy-target"]))
                self.prepare()
                self.assert_legacy_preserved(original_device=original)
                self.assertIn("light.my_patio", self.warned)

    def test_parent_identifier_from_another_account_is_not_identity_evidence(self):
        self.devices.devices["legacy-controller"].identifiers = {("haven", "haven:other:location:123456")}
        original = deepcopy(vars(self.devices.devices["legacy-target"]))
        self.prepare()
        self.assert_legacy_preserved(original_device=original)

    def test_shared_legacy_device_is_retained_without_moving_its_entity(self):
        self.devices.devices["legacy-target"].config_entries.add("entry-two")
        original = deepcopy(vars(self.devices.devices["legacy-target"]))
        self.prepare()
        self.assert_legacy_preserved(original_device=original)
        self.assertIn("light.my_patio", self.warned)

    def test_existing_target_device_does_not_steal_the_legacy_entity(self):
        self.devices.add("existing-target", {("haven", "haven:42:123456:zone:101")})
        original = deepcopy(vars(self.devices.devices["legacy-target"]))
        self.prepare()
        self.assert_legacy_preserved(original_device=original)
        self.assertIn("light.my_patio", self.warned)

    def test_other_entry_namespaced_device_blocks_before_any_registry_mutation(self):
        for identifier in ["haven:42:123456:zone:101", "haven:42:location:123456"]:
            with self.subTest(identifier=identifier):
                self.setUp()
                self.devices.add("other-entry-device", {("haven", identifier)}, owners=("entry-two",))
                before = {key: deepcopy(vars(value)) for key, value in self.devices.devices.items()}
                with self.assertRaises(self.subject.ConfigEntryError):
                    self.prepare()
                self.assertEqual(self.devices.calls, [])
                self.assertEqual(self.entities.calls, [])
                self.assertEqual(
                    {key: vars(value) for key, value in self.devices.devices.items()}, before
                )
                self.assert_legacy_preserved()

    def test_missing_legacy_raw_identifier_does_not_change_device_id(self):
        self.devices.devices["legacy-target"].identifiers = {("haven", "unknown-device")}
        original = deepcopy(vars(self.devices.devices["legacy-target"]))
        self.prepare()
        self.assert_legacy_preserved(original_device=original)
        self.assertIn("light.my_patio", self.warned)

    def test_other_entry_namespaced_entity_blocks_before_any_registry_mutation(self):
        self.entities.add(
            "light.other_entry", "haven:42:123456:zone:101", None, entry_id="entry-two"
        )
        before = {key: deepcopy(vars(value)) for key, value in self.devices.devices.items()}
        with self.assertRaises(self.subject.ConfigEntryError):
            self.prepare()
        self.assertEqual(self.devices.calls, [])
        self.assertEqual(self.entities.calls, [])
        self.assertEqual({key: vars(value) for key, value in self.devices.devices.items()}, before)
        self.assert_legacy_preserved()

    def test_existing_namespaced_entity_is_not_overwritten(self):
        self.entities.add("light.existing", "haven:42:123456:zone:101", None)
        original = deepcopy(vars(self.devices.devices["legacy-target"]))
        self.prepare()
        self.assert_legacy_preserved(original_device=original)
        self.assertEqual(self.entities.entities["light.existing"].unique_id, "haven:42:123456:zone:101")

    def test_missing_target_can_migrate_when_it_is_discovered_later(self):
        available = self.coordinator.data
        self.coordinator.data = {}
        self.prepare()
        self.assert_legacy_preserved()
        self.assertEqual(set(self.devices.devices), {"legacy-controller", "legacy-target"})
        self.coordinator.data = available
        self.prepare()
        self.assertEqual(self.entities.entities["light.my_patio"].unique_id, "haven:42:123456:zone:101")
        self.assertEqual(self.entities.entities["light.my_patio"].device_id, "legacy-target")
        self.assertEqual(set(self.devices.devices), {"legacy-controller", "legacy-target"})

    def test_modern_single_owner_devices_preserve_the_same_identity(self):
        for device in self.devices.devices.values():
            device.config_entry_id = "entry-one"
        self.prepare()
        self.assertEqual(self.entities.entities["light.my_patio"].device_id, "legacy-target")
        self.assertEqual(self.entities.entities["light.my_patio"].unique_id, "haven:42:123456:zone:101")


if __name__ == "__main__":
    unittest.main()
