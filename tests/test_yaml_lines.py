"""The one loader for configuration files (chaperone/yaml_lines.py): values
read as written, a value plain YAML would read as another type refused with its line, explicit tags refused, and a key
given two different values refused with both places, merges included; the same value twice loads."""

import re
from pathlib import Path

import pytest
import yaml

from chaperone.yaml_lines import Conflict, read_yaml, same_value


@pytest.mark.parametrize("written, read", [
    ("true", True), ("True", True), ("TRUE", True), ("false", False), ("False", False), ("FALSE", False),
    ("12", 12), ("-3", -3), ("0", 0), ("1.5", 1.5), ("5e-3", 0.005), ("1e3", 1000.0), ("-2.5E+2", -250.0),
    ("null", None), ("~", None), ("", None), ("NA", "NA"), ("0o17", "0o17"), ("hello world", "hello world"),
    ('"123"', "123"), ('"yes"', "yes"), ("'.5'", ".5"), ('"2026-10-29"', "2026-10-29"), ('"1:30"', "1:30"),
])
def test_a_value_is_read_as_written(write, written, read):
    """Only true and false are true/false, a number only as JSON writes it, null is null; everything else is text."""
    value = read_yaml(write("v.yaml", f"v: {written}\n"))["v"]
    assert same_value(value, read), (written, value)


@pytest.mark.parametrize("written, meant, fix", [
    ("yes", "true/false", "write true or false"), ("no", "true/false", "write true or false"),
    ("on", "true/false", "write true or false"), ("Off", "true/false", "write true or false"),
    (".5", "a number", "write the number as JSON does"), ("012", "a number", "write the number as JSON does"),
    ("1:30", "a number", "write the number as JSON does"), ("0x1F", "a number", "write the number as JSON does"),
    ("1_000", "a number", "write the number as JSON does"), ("+1", "a number", "write the number as JSON does"),
    (".inf", "a number", "write the number as JSON does"), (".nan", "a number", "write the number as JSON does"),
    ("2026-10-29", "a date", "quote it"),
])
def test_a_value_plain_yaml_reads_as_another_type_is_refused_naming_its_line(write, written, meant, fix):
    """Where this loader and plain YAML would read a value differently, the file must say which it means:
    `no` in a workflow would otherwise be the text "no", which a branch takes as true."""
    path = write("v.yaml", f"a: 1\nv: {written}\n")
    with pytest.raises(yaml.YAMLError) as raised:
        read_yaml(path)
    said = str(raised.value)
    assert f"'{written}' is {meant} to plain YAML and text here: {fix}" in said and f'quote it: "{written}"' in said
    assert f'"{path}", line 2' in said


def test_a_key_plain_yaml_reads_as_another_type_is_refused_too(write):
    with pytest.raises(yaml.YAMLError, match="'yes' is true/false to plain YAML"):
        read_yaml(write("k.yaml", "yes: 1\n"))


@pytest.mark.parametrize("written", ["!!int 3", "!!str x", "!!bool yes", "!!float .nan", "!!timestamp 2026-10-29",
                                     "!!omap [x: 1, x: 2]", "!!set {a: null}"])
def test_an_explicit_tag_is_refused(write, written):
    """A value's type is how it is written; a tag would bring back plain YAML's readings (!!int 012 is 10)."""
    with pytest.raises(yaml.YAMLError, match=f"the tag {re.escape(written.split()[0])} is refused"):
        read_yaml(write("t.yaml", f"v: {written}\n"))


def test_a_number_too_large_is_refused_not_read_as_infinity(write):
    with pytest.raises(yaml.YAMLError, match="'1e400' is too large to be a number"):
        read_yaml(write("n.yaml", "v: 1e400\n"))


def test_a_key_given_two_different_values_is_refused_naming_both_lines(write):
    path = write("a.yaml", "a: 1\nb: 2\na: 3\n")
    with pytest.raises(Conflict) as raised:
        read_yaml(path)
    assert str(raised.value) == (f"{path}:3: 'a' is given two different values, at line 1 and line 3; plain YAML would "
                                 "silently keep the second. Write it once")


def test_the_same_value_twice_loads(write):
    assert read_yaml(write("a.yaml", "a: [1, {b: true}]\nc: 2\na: [1, {b: true}]\n")) == {"a": [1, {"b": True}], "c": 2}


def test_true_and_one_are_different_values(write):
    with pytest.raises(Conflict):
        read_yaml(write("a.yaml", "a: true\na: 1\n"))


@pytest.mark.parametrize("first, again", [("1", "true"), ("1", "1.0"), ("0", "false")])
def test_two_keys_python_counts_as_one_are_refused(write, first, again):
    """1, 1.0 and true are one key to a Python mapping: one would silently replace the other."""
    for second in ("a", "b"):                                   # the same value or another: both refused
        with pytest.raises(Conflict, match=f"the keys {first} \\(line 1\\) and {again} \\(line 2\\) are one key to "
                                           "the program"):
            read_yaml(write("k.yaml", f"{first}: a\n{again}: {second}\n"))


def test_a_mapping_with_a_key_of_another_type_is_another_value(write):
    assert not same_value({1: "x"}, {True: "x"}) and same_value({1: "x"}, {1: "x"})
    with pytest.raises(Conflict, match="'a' is given two different values"):
        read_yaml(write("m.yaml", "a: {1: x}\na: {true: x}\n"))


def test_a_key_is_named_as_yaml_writes_it(write):
    with pytest.raises(Conflict, match="'null' is given two different values"):
        read_yaml(write("n.yaml", "~: a\nnull: b\n"))


def test_two_values_on_one_line_are_told_apart_by_column(write):
    with pytest.raises(Conflict, match="at line 1:2 and line 1:8"):              # where the keys are written
        read_yaml(write("a.yaml", "{a: 1, a: 2}\n"))


def test_a_repeat_names_where_its_keys_are_written(write):
    """Not the anchor of an aliased value, and not a value on the line below its key."""
    with pytest.raises(Conflict, match="at line 2 and line 4"):
        read_yaml(write("a.yaml", "base: &v 1\na: 2\nother: 3\na: *v\n"))
    with pytest.raises(Conflict, match="at line 1 and line 3"):
        read_yaml(write("b.yaml", "A:\n  next: B\nA:\n  next: C\n"))


def test_an_empty_file_is_an_empty_mapping_and_a_file_of_null_is_null(write):
    assert read_yaml(write("e.yaml", "")) == {} and read_yaml(write("c.yaml", "# only a comment\n")) == {}
    assert read_yaml(write("n.yaml", "null\n")) is None and read_yaml(write("l.yaml", "[]\n")) == []


def test_a_file_that_is_not_utf8_is_refused_naming_the_file(tmp_path):
    path = tmp_path / "latin1.yaml"
    path.write_bytes("a: caf\u00e9\n".encode("latin-1"))
    with pytest.raises(yaml.YAMLError, match=f'in "{path}", position 6'):
        read_yaml(path)


def test_a_merged_value_given_again_differently_is_refused_and_given_again_the_same_loads(write):
    base = "base: &b {x: 1, y: 2}\n"
    with pytest.raises(Conflict, match="'x' is given two different values"):
        read_yaml(write("m.yaml", base + "item:\n  <<: *b\n  x: 5\n"))
    assert read_yaml(write("s.yaml", base + "item:\n  <<: *b\n  x: 1\n"))["item"] == {"x": 1, "y": 2}


def test_every_mapping_knows_the_line_of_each_key(write):
    data = read_yaml(write("l.yaml", "top:\n  first: 1\n  second:\n    - a\n    - b\n"))
    assert data.key_lines == {"top": 1} and data["top"].key_lines == {"first": 2, "second": 3}
    assert data["top"]["second"].item_lines == [4, 5]


# The places that read a configuration file: each through `read_yaml` (or `load_yaml`), so no file is read the plain
# YAML way.
ROOT = Path(__file__).resolve().parents[1]
LEFT: list[tuple[str, str]] = []


def test_every_configuration_file_is_read_by_the_one_loader():
    found = []
    for top in ("chaperone", "examples", "scripts"):
        for path in sorted((ROOT / top).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel == "chaperone/yaml_lines.py":
                continue
            found += [(rel, call) for call in re.findall(r"yaml\.(?:safe_load|full_load|unsafe_load|load|compose)\b",
                                                         path.read_text(encoding="utf-8"))]
    assert sorted(found) == sorted(LEFT)                       # each once: a second one in the same file is caught too
