"""Physical KiCad stackup semantics and bounded electrical estimator checks."""
from dataclasses import FrozenInstanceError
import math

import pytest

from velatrace.errors import ValidationError
from velatrace.stackup import homogeneous_delay_ps, read_stackup, thin_microstrip


def board_text(count=4, *, legacy=False):
    # KiCad 9/10 serializes layer-table order by ID: B.Cu appears BEFORE inner copper.
    names = ["F.Cu", "B.Cu", *(f"In{i}.Cu" for i in range(1, count - 1))]
    ids = [0, 31 if legacy else 2, *(i if legacy else 2 * i + 2
                                      for i in range(1, count - 1))]
    table = " ".join(f'({number} "{name}" signal)' for name, number in zip(names, ids))
    physical = ["F.Cu", *(f"In{i}.Cu" for i in range(1, count - 1)), "B.Cu"]
    layers = ['(layer "F.Mask" (type "Top Solder Mask") (thickness 0.01) '
              '(epsilon_r 3.3) (loss_tangent 0.02))']
    for index, name in enumerate(physical):
        if index:
            layers.append(f'(layer "dielectric {index}" (type "core") '
                          f'(thickness {index / 10}) (material "FR4") '
                          '(epsilon_r 4) (loss_tangent 0.02))')
        layers.append(f'(layer "{name}" (type "copper") (thickness 0.035))')
    layers.append('(layer "B.Mask" (type "Bottom Solder Mask") (thickness 0.01))')
    return ('(kicad_pcb (version 20241229) (general (thickness 1.6)) '
            f'(layers {table}) (setup (stackup {" ".join(layers)} '
            '(copper_finish "ENIG") (dielectric_constraints no))))')


@pytest.mark.parametrize("count", [2, 4, 6, 8, 32])
@pytest.mark.parametrize("legacy", [False, True])
def test_physical_copper_order_ignores_layer_id_order(count, legacy):
    stackup = read_stackup(board_text(count, legacy=legacy))
    assert stackup.copper_layers == ("F.Cu", *(f"In{i}.Cu" for i in range(1, count - 1)), "B.Cu")
    centers = [stackup.copper_z_nm(name) for name in stackup.copper_layers]
    assert centers == sorted(centers)
    assert centers[0] == 17_500  # Mask does not move the F.Cu face origin.
    total_dielectric_nm = sum(range(1, count)) * 100_000
    assert centers[-1] == total_dielectric_nm + count * 35_000 - 17_500
    assert stackup.separation_nm("F.Cu", "B.Cu") == centers[-1] - centers[0]
    assert stackup.separation_nm("B.Cu", "F.Cu") == centers[-1] - centers[0]


def test_bare_addsublayer_preserves_distinct_materials_and_locked_thickness():
    board = board_text().replace(
        '(thickness 0.1) (material "FR4") (epsilon_r 4) (loss_tangent 0.02)',
        '(thickness 0.08 locked) (material "Core A") (epsilon_r 4.2) '
        '(loss_tangent 0.015) addsublayer (thickness 0.02) (material "Resin B") '
        '(epsilon_r 3.6) (loss_tangent 0.03)')
    stackup = read_stackup(board)
    dielectric = next(layer for layer in stackup.layers if layer.name == "dielectric 1")
    assert dielectric.thickness_nm == 100_000
    assert [part.thickness_nm for part in dielectric.sublayers] == [80_000, 20_000]
    assert [part.epsilon_r for part in dielectric.sublayers] == [4.2, 3.6]
    assert [part.loss_tangent for part in dielectric.sublayers] == [0.015, 0.03]
    assert [part.material for part in dielectric.sublayers] == ["Core A", "Resin B"]
    assert [part.thickness_locked for part in dielectric.sublayers] == [True, False]
    assert stackup.separation_nm("F.Cu", "In1.Cu") == 135_000


def test_missing_stackup_does_not_invent_uniform_fr4_from_general_thickness():
    board = '(kicad_pcb (general (thickness 1.6)) (layers (0 "F.Cu" signal) (2 "B.Cu" signal)))'
    stackup = read_stackup(board)
    assert not stackup.has_stackup
    assert stackup.layers == ()
    assert stackup.copper_layers == ("F.Cu", "B.Cu")
    assert stackup.copper_z_nm("B.Cu") is None
    assert stackup.separation_nm("F.Cu", "B.Cu") is None


def test_missing_material_properties_remain_unknown_with_known_geometry():
    board = board_text().replace('(material "FR4") (epsilon_r 4) (loss_tangent 0.02)', '')
    stackup = read_stackup(board)
    parts = [part for layer in stackup.layers if layer.kind == "dielectric"
             for part in layer.sublayers]
    assert all(part.material is None and part.epsilon_r is None and part.loss_tangent is None
               for part in parts)
    assert stackup.separation_nm("F.Cu", "In1.Cu") == 135_000


def test_unknown_upstream_thickness_does_not_poison_independent_inner_span():
    stackup = read_stackup(board_text().replace(
        '(layer "F.Cu" (type "copper") (thickness 0.035))',
        '(layer "F.Cu" (type "copper"))'))
    assert stackup.copper_z_nm("F.Cu") is None
    assert stackup.copper_z_nm("B.Cu") is None
    assert stackup.separation_nm("F.Cu", "B.Cu") is None
    assert stackup.separation_nm("In1.Cu", "In2.Cu") == 235_000
    assert stackup.separation_nm("In1.Cu", "In1.Cu") == 0


def test_one_unknown_sublayer_makes_combined_thickness_unknown():
    board = board_text().replace('(thickness 0.1)', '(thickness 0.08) addsublayer')
    stackup = read_stackup(board)
    layer = next(layer for layer in stackup.layers if layer.kind == "dielectric")
    assert layer.thickness_nm is None
    assert stackup.separation_nm("F.Cu", "In1.Cu") is None


def test_stackup_is_immutable_and_fingerprint_is_semantic():
    original = board_text()
    stackup = read_stackup(original)
    with pytest.raises(FrozenInstanceError):
        stackup.layers = ()
    with pytest.raises(FrozenInstanceError):
        stackup.layers[0].sublayers[0].epsilon_r = 99
    equivalent = original.replace('(thickness 0.035)', '(thickness 0.035000)')
    equivalent = equivalent.replace('(epsilon_r 4)', '(epsilon_r 4.000)')
    equivalent = equivalent.replace('(setup', '\n (net 0 "")\n(setup')
    assert read_stackup(equivalent).fingerprint == stackup.fingerprint
    assert len(stackup.fingerprint) == 64
    for before, after in [('(epsilon_r 4)', '(epsilon_r 4.1)'),
                          ('(thickness 0.035)', '(thickness 0.036)'),
                          ('"FR4"', '"different material"')]:
        assert read_stackup(original.replace(before, after)).fingerprint != stackup.fingerprint


@pytest.mark.parametrize("before,after", [
    ('(0 "F.Cu" signal)', '(0 "F.Cu" signal) (0 "F.Cu" signal)'),
    ('(4 "In1.Cu" signal)', '(4 "In3.Cu" signal)'),
    ('(4 "In1.Cu" signal)', '(4 "In01.Cu" signal)'),
    ('(4 "In1.Cu" signal)', '(7 "In1.Cu" signal)'),
    ('(4 "In1.Cu" signal)', '(4 "Inner" signal)'),
    ('(0 "F.Cu" signal)', '(0 "F.Cu" user)'),
    ('(0 "F.Cu" signal)', ''),
    ('(2 "B.Cu" signal)', ''),
    ('(0 "F.Cu" signal)', '(zero "F.Cu" signal)'),
    ('(0 "F.Cu" signal)', '(0 "F.Cu" (signal))'),
])
def test_malformed_or_inconsistent_canonical_copper_lists_rejected(before, after):
    with pytest.raises(ValidationError):
        read_stackup(board_text().replace(before, after))


def test_odd_copper_count_rejected():
    with pytest.raises(ValidationError):
        read_stackup(board_text(3))


@pytest.mark.parametrize("before,after", [
    ('(layer "In1.Cu"', '(layer "In2.Cu"'),
    ('(layer "In1.Cu"', '(layer "In3.Cu"'),
    ('(layer "dielectric 1"', '(layer "dielectric 2"'),
    ('(layer "dielectric 1"', '(layer "mystery"'),
    ('(layer "F.Mask"', '(layer "B.Paste"'),
    ('(type "copper")', '(type "core")'),
    ('(type "core")', '(type "copper")'),
    ('(epsilon_r 4)', '(epsilon_r 4) (epsilon_r 3)'),
    ('(epsilon_r 4)', '(epsilon_r 4 extra)'),
    ('(epsilon_r 4)', '(epsilon_r (value 4))'),
    ('(epsilon_r 4)', '(dielectric_model wideband)'),
    ('(type "core")', '(type "core") (type "core")'),
    ('(thickness 0.1)', '(thickness 0.1 unknown)'),
    ('(thickness 0.035)', '(thickness 0.035) addsublayer (thickness 0.035)'),
    ('(loss_tangent 0.02))', '(loss_tangent 0.02) addsublayer)'),
    ('(thickness 0.1)', '(thickness 0.1) "addsublayer"'),
    ('(stackup', '(stackup) (stackup'),
    ('(stackup', '(stackup unexpected'),
    ('(stackup', '(stackup (dielectric_model future-model)'),
    ('(dielectric_constraints no)', '(dielectric_constraints unknown)'),
    ('(dielectric_constraints no)', '(dielectric_constraints no) (dielectric_constraints no)'),
    ('(setup', '(setup) (setup'),
])
def test_inconsistent_or_unsupported_stackup_structure_rejected(before, after):
    with pytest.raises(ValidationError):
        read_stackup(board_text().replace(before, after))


def test_stackup_reversed_inner_copper_rejected():
    text = board_text().replace('(layer "In1.Cu"', '(layer "TEMP"')
    text = text.replace('(layer "In2.Cu"', '(layer "In1.Cu"')
    text = text.replace('(layer "TEMP"', '(layer "In2.Cu"')
    with pytest.raises(ValidationError, match="physical order"):
        read_stackup(text)


def test_missing_dielectric_rejected():
    text = board_text().replace('(layer "dielectric 1" (type "core") (thickness 0.1) '
                                '(material "FR4") (epsilon_r 4) (loss_tangent 0.02))', '')
    with pytest.raises(ValidationError, match="dielectric layer"):
        read_stackup(text)


@pytest.mark.parametrize("thickness", ["0", "-0.1", "nan", "inf", "oops", "0.0000001", "1001",
                                        "0.1234560000000000000000000000001", "1e999999999"])
def test_bad_copper_or_dielectric_thickness_rejected(thickness):
    with pytest.raises(ValidationError):
        read_stackup(board_text().replace('(thickness 0.1)', f'(thickness {thickness})'))


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "oops"])
def test_bad_dielectric_constants_rejected(value):
    with pytest.raises(ValidationError):
        read_stackup(board_text().replace('(epsilon_r 4)', f'(epsilon_r {value})'))


@pytest.mark.parametrize("value", ["-0.01", "nan", "inf", "oops"])
def test_bad_loss_tangent_rejected(value):
    with pytest.raises(ValidationError):
        read_stackup(board_text().replace('(loss_tangent 0.02)', f'(loss_tangent {value})'))


def test_nm_resolution_and_zero_surface_thickness():
    stackup = read_stackup(board_text().replace('(thickness 0.035)', '(thickness 0.000001)')
                          .replace('(thickness 0.01)', '(thickness 0)'))
    assert stackup.copper_z_nm("F.Cu") == 0.5
    assert stackup.separation_nm("F.Cu", "In1.Cu") == 100_001


def test_nonzero_surface_thickness_cannot_underflow_to_zero():
    with pytest.raises(ValidationError):
        read_stackup(board_text().replace('(thickness 0.01)', '(thickness 1e-999999999)'))


def test_query_rejects_undeclared_reference_plane():
    stackup = read_stackup(board_text())
    with pytest.raises(ValidationError):
        stackup.copper_z_nm("GND")
    with pytest.raises(ValidationError):
        stackup.separation_nm("F.Cu", "In3.Cu")


def test_homogeneous_delay_analytic_values_and_segment_sum():
    assert homogeneous_delay_ps(1, 1) == pytest.approx(3.3356409519815204)
    assert homogeneous_delay_ps(1, 4) == pytest.approx(6.671281903963041)
    assert homogeneous_delay_ps(30, 4) == pytest.approx(
        homogeneous_delay_ps(10, 4) + homogeneous_delay_ps(20, 4))
    assert homogeneous_delay_ps(0, 4) == 0
    # Geometrically matched traces can have different electrical lengths.
    assert homogeneous_delay_ps(30, 4) != homogeneous_delay_ps(30, 3)


@pytest.mark.parametrize("length,er", [(-1, 4), (math.inf, 4), (1, math.nan), (1, 0.9),
                                      (True, 4), (1, True), (1e308, 4)])
def test_homogeneous_delay_refuses_invalid_inputs_and_overflow(length, er):
    with pytest.raises(ValidationError):
        homogeneous_delay_ps(length, er)


def test_microstrip_air_limit_and_analytic_delay():
    air = thin_microstrip(1, 1, 1)
    assert air.effective_epsilon_r == 1
    assert air.delay_ps_per_mm == pytest.approx(3.3356409519815204)
    substrate = thin_microstrip(1, 1, 4)
    assert 1 < substrate.effective_epsilon_r < 4
    assert substrate.impedance_ohm == pytest.approx(
        air.impedance_ohm / math.sqrt(substrate.effective_epsilon_r))
    assert substrate.delay_ps_per_mm == pytest.approx(
        homogeneous_delay_ps(1, substrate.effective_epsilon_r))
    assert "zero-thickness" in substrate.model
    assert any("no solder mask" in assumption for assumption in substrate.assumptions)


def test_microstrip_scale_invariance_and_width_monotonicity():
    original = thin_microstrip(0.2, 0.1, 4)
    scaled = thin_microstrip(2, 1, 4)
    assert original == scaled
    assert thin_microstrip(0.4, 0.1, 4).impedance_ohm < original.impedance_ohm
    assert thin_microstrip(0.2, 0.1, 6).impedance_ohm < original.impedance_ohm


@pytest.mark.parametrize("ratio", [0.01, 0.1, 1, 10, 100])
@pytest.mark.parametrize("er", [1, 4, 127.99])
def test_microstrip_domain_boundaries_are_finite(ratio, er):
    estimate = thin_microstrip(ratio, 1, er)
    assert math.isfinite(estimate.impedance_ohm) and estimate.impedance_ohm > 0
    assert 1 <= estimate.effective_epsilon_r <= er


@pytest.mark.parametrize("width,height,er", [
    (0.00999, 1, 4), (100.001, 1, 4), (1, 1, 128), (1, 1, 0.9),
    (0, 1, 4), (1, 0, 4), (-1, 1, 4), (math.inf, 1, 4),
    (1, math.nan, 4), (1, 1, math.inf), (True, 1, 4), (1, 1, True),
])
def test_microstrip_refuses_unsupported_model_inputs(width, height, er):
    with pytest.raises(ValidationError):
        thin_microstrip(width, height, er)
