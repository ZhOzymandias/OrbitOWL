"""Bounded section output and validation against the original text."""

import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation

from .contracts import ServiceError

PREDICATES = {
    "prerequisite": "richiedePrerequisito",
    "condition": "haCondizioneApplicabilita",
    "exception": "haEccezione",
}
ENTITY_KINDS = {"class", "individual", "procedure"}
RELATION_KINDS = {"subclass", "type", "relation", *PREDICATES}
VALUE_KINDS = {"text", "integer", "decimal", "boolean"}
LIMITS = {"entities": 32, "relations": 32, "attributes": 24}

# Preserved verbatim for computing 1.4 / 1.6.1 / 1.6.2 checkpoint signatures.
LEGACY_GRAMMAR = r'''
root ::= ws complete ws
complete ::= "{" ws "\"status\"" sep "\"complete\"" comma "\"entities\"" sep entities comma "\"relations\"" sep relations comma "\"attributes\"" sep attributes ws "}"
entities ::= "[" ws ( entity (comma entity){0,13} )? ws "]"
relations ::= "[" ws ( relation (comma relation){0,11} )? ws "]"
attributes ::= "[" ws ( attribute (comma attribute){0,7} )? ws "]"
entity ::= "{" ws "\"id\"" sep key comma "\"kind\"" sep ("\"class\"" | "\"individual\"" | "\"procedure\"") comma "\"label\"" sep short comma "\"scope\"" sep short ws "}"
relation ::= "{" ws "\"source\"" sep key comma "\"kind\"" sep ("\"subclass\"" | "\"type\"" | "\"relation\"" | "\"prerequisite\"" | "\"condition\"" | "\"exception\"") comma "\"label\"" sep short comma "\"target\"" sep key comma "\"quote\"" sep quote ws "}"
attribute ::= "{" ws "\"source\"" sep key comma "\"label\"" sep short comma "\"type\"" sep ("\"text\"" | "\"integer\"" | "\"decimal\"" | "\"boolean\"") comma "\"value\"" sep short comma "\"quote\"" sep quote ws "}"
key ::= "\"" [a-z] [a-z0-9_]{0,15} "\""
short ::= "\"" char{0,120} "\""
quote ::= "\"" char{1,360} "\""
char ::= [^"\\\x00-\x1F] | "\\" (["\\/bfnrt] | "u" [0-9a-fA-F]{4})
comma ::= ws "," ws
sep ::= ws ":" ws
ws ::= [ \t\n\r]{0,8}
'''.strip()

GRAMMAR = r'''
root ::= ws complete ws
complete ::= "{" ws "\"status\"" sep "\"complete\"" comma "\"entities\"" sep entities comma "\"relations\"" sep relations comma "\"attributes\"" sep attributes ws "}"
entities ::= "[" ws ( entity (comma entity){0,31} )? ws "]"
relations ::= "[" ws ( relation (comma relation){0,31} )? ws "]"
attributes ::= "[" ws ( attribute (comma attribute){0,23} )? ws "]"
entity ::= "{" ws "\"id\"" sep key comma "\"kind\"" sep ("\"class\"" | "\"individual\"" | "\"procedure\"") comma "\"label\"" sep short-nonempty comma "\"scope\"" sep short ws "}"
relation ::= "{" ws "\"source\"" sep key comma "\"kind\"" sep ("\"subclass\"" | "\"type\"" | "\"relation\"" | "\"prerequisite\"" | "\"condition\"" | "\"exception\"") comma "\"label\"" sep short comma "\"target\"" sep key comma "\"quote\"" sep quote ws "}"
attribute ::= "{" ws "\"source\"" sep key comma "\"label\"" sep short-nonempty comma "\"type\"" sep ("\"text\"" | "\"integer\"" | "\"decimal\"" | "\"boolean\"") comma "\"value\"" sep short-nonempty comma "\"quote\"" sep quote ws "}"
key ::= "\"" [a-z] [a-z0-9_]{0,15} "\""
short ::= "\"" char{0,120} "\""
short-nonempty ::= "\"" char{1,120} "\""
int-val ::= "\"" ("+" | "-")? [0-9]{1,24} "\""
dec-val ::= "\"" ("+" | "-")? [0-9]{1,24} ("." [0-9]{1,12})? "\""
bool-val ::= "\"true\"" | "\"false\""
quote ::= "\"" char{1,360} "\""
char ::= [^"\\\x00-\x1F] | "\\" (["\\/bfnrt] | "u" [0-9a-fA-F]{4})
comma ::= ws "," ws
sep ::= ws ":" ws
ws ::= [ \t\n\r]{0,8}
'''.strip()


def grammar_rules(source=GRAMMAR):
    return dict(line.split(" ::= ", 1) for line in source.splitlines())


def render_grammar(rules):
    return "\n".join(f"{name} ::= {rule}" for name, rule in rules.items())


def json_fields(*fields):
    return " comma ".join(f"{json.dumps(json.dumps(key))} sep {rule}" for key, rule in fields)


def json_object(*fields):
    return '"{" ws ' + json_fields(*fields) + ' ws "}"'


def choices(values):
    return " | ".join(json.dumps(json.dumps(value)) for value in sorted(values))


def _legacy_allowed_relation(kind, source, target):
    if kind == "subclass":
        return source == target == "class"
    if kind == "type":
        return source != "class" and target == "class"
    if kind == "relation":
        return (source == "class") == (target == "class")
    return kind in PREDICATES and source == "procedure" and target != "class"


def allowed_relation(kind, source, target):
    if kind == "subclass":
        return source == target == "class"
    if kind == "type":
        return source != "class" and target == "class"
    if kind == "relation":
        return (source == "class") == (target == "class")
    # Operational predicates apply from procedures or classes to any entity kind (compiled as OWL 2 DL).
    return kind in PREDICATES and source in {"procedure", "class"}


def legacy_entity_grammar_1_6():
    rules = grammar_rules(LEGACY_GRAMMAR)
    rules["root"] = "ws " + json_object(("entities", "entities")) + " ws"
    return render_grammar(rules)


def legacy_fact_grammar_1_6(entities, passages=None):
    entities = {row["id"]: row["kind"] for row in entities}
    rules = grammar_rules(LEGACY_GRAMMAR)
    rules["root"] = (
        "ws "
        + json_object(
            ("relations", "relations"), ("attributes", "attributes"), ("unresolved", "unresolved")
        )
        + " ws"
    )
    rules["unresolved"] = '"[" ws ( quote (comma quote){0,11} )? ws "]"'
    rules.pop("complete")
    combinations = []
    for source_kind in sorted(set(entities.values())):
        sources = [key for key, kind in entities.items() if kind == source_kind]
        for kind in sorted(RELATION_KINDS):
            targets = [
                key
                for key, target_kind in entities.items()
                if _legacy_allowed_relation(kind, source_kind, target_kind)
            ]
            if targets:
                combinations.append(
                    json_object(
                        ("source", f"({choices(sources)})"),
                        ("kind", choices([kind])),
                        ("label", "short"),
                        ("target", f"({choices(targets)})"),
                        ("quote", "quote"),
                    )
                )
    if combinations:
        rules["relation"] = " | ".join(combinations)
    else:
        rules["relations"] = '"[" ws "]"'
        rules.pop("relation")
    if entities:
        rules["attribute"] = rules["attribute"].replace("sep key", f"sep ({choices(entities)})", 1)
    else:
        rules["attributes"] = '"[" ws "]"'
        rules.pop("attribute")
    if passages is not None:
        rules["quote"] = choices(passages)
    return render_grammar(rules)


def entity_grammar():
    rules = grammar_rules()
    rules["root"] = "ws " + json_object(("entities", "entities")) + " ws"
    return render_grammar(rules)


def fact_grammar(entities, passages=None):
    entities = {row["id"]: row["kind"] for row in entities}
    rules = grammar_rules()
    rules["root"] = (
        "ws "
        + json_object(
            ("relations", "relations"), ("attributes", "attributes"), ("unresolved", "unresolved")
        )
        + " ws"
    )
    rules["unresolved"] = '"[" ws ( quote (comma quote){0,15} )? ws "]"'
    rules.pop("complete")
    combinations = []
    for source_kind in sorted(set(entities.values())):
        sources = [key for key, kind in entities.items() if kind == source_kind]
        for kind in sorted(RELATION_KINDS):
            targets = [
                key
                for key, target_kind in entities.items()
                if allowed_relation(kind, source_kind, target_kind)
            ]
            if targets:
                label_rule = "short-nonempty" if kind == "relation" else choices([""])
                combinations.append(
                    json_object(
                        ("source", f"({choices(sources)})"),
                        ("kind", choices([kind])),
                        ("label", label_rule),
                        ("target", f"({choices(targets)})"),
                        ("quote", "quote"),
                    )
                )
    if combinations:
        rules["relation"] = " | ".join(combinations)
    else:
        rules["relations"] = '"[" ws "]"'
        rules.pop("relation")
    if entities:
        ent_choices = f"({choices(entities)})"
        typed_attrs = [
            json_object(
                ("source", ent_choices),
                ("label", "short-nonempty"),
                ("type", choices([typ])),
                ("value", val_rule),
                ("quote", "quote"),
            )
            for typ, val_rule in (
                ("text", "short-nonempty"),
                ("integer", "int-val"),
                ("decimal", "dec-val"),
                ("boolean", "bool-val"),
            )
        ]
        rules["attribute"] = " | ".join(typed_attrs)
    else:
        rules["attributes"] = '"[" ws "]"'
        rules.pop("attribute")
    if passages:
        rules["quote"] = choices(passages)
    return render_grammar(rules)


def source_passages(text):
    """Number contiguous source passages within the existing citation length limit."""
    passages = {}
    for part in re.split(r"\n[ \t]*\n+|(?<=[.!?])\s+", text):
        rest = clean(part)
        while rest:
            end = len(rest) if len(rest) <= 360 else rest.rfind(" ", 0, 361)
            if end <= 0:
                end = 360
            passages[f"p{len(passages) + 1}"] = rest[:end]
            rest = rest[end:].lstrip()
    return passages


def clean(value):
    return " ".join(unicodedata.normalize("NFC", value).split())


def identity(value):
    return unicodedata.normalize("NFKC", clean(value)).casefold()


def entity_key(row):
    """Canonical identity by (kind, normalized_label); scope is preserved as pc:ambito."""
    return row["kind"], identity(row["label"])


def validate_section(data, source):
    if isinstance(data, dict) and data.get("status") == "split":
        raise ServiceError("section_output_contract_violation")
    if not isinstance(data, dict) or set(data) != {"status", *LIMITS} or data["status"] != "complete":
        raise ServiceError("section_schema_invalid")
    if any(not isinstance(data[k], list) or len(data[k]) > n for k, n in LIMITS.items()):
        raise ServiceError("section_schema_invalid")
    # An answer at a grammar limit may have omitted information to fit the array.
    full = {key: limit for key, limit in LIMITS.items() if len(data[key]) == limit}
    if full:
        raise ServiceError("section_capacity_reached", details={"full_lists": full})

    def record(row, fields):
        if not isinstance(row, dict) or set(row) != set(fields.split()) or any(
            not isinstance(s, str)
            or any(
                ord(c) < 32
                and c not in "\t\n\r"
                or 0xD800 <= ord(c) <= 0xDFFF
                or ord(c) in (0xFFFE, 0xFFFF)
                for c in s
            )
            for s in row.values()
        ):
            raise ServiceError("section_schema_invalid")

    entities = {}
    for entity in data["entities"]:
        record(entity, "id kind label scope")
        key = entity["id"]
        if (
            not re.fullmatch(r"[a-z][a-z0-9_]{0,15}", key)
            or key in entities
            or entity["kind"] not in ENTITY_KINDS
            or not clean(entity["label"])
            or max(len(entity["label"]), len(entity["scope"])) > 120
        ):
            raise ServiceError("section_schema_invalid")
        entities[key] = entity["kind"]

    def evidence(row, location):
        quote = clean(row["quote"])
        if not quote or len(row["quote"]) > 360 or quote not in clean(source):
            raise ServiceError("section_quote_not_found", details={**location, "field": "quote"})

    for index, relation in enumerate(data["relations"]):
        location = {"list": "relations", "index": index}
        record(relation, "source kind label target quote")
        source_kind = entities.get(relation["source"])
        target_kind = entities.get(relation["target"])
        kind = relation["kind"]
        if not source_kind or not target_kind:
            raise ServiceError(
                "section_relation_entity_unknown",
                details={
                    **location,
                    "missing_endpoints": [
                        field for field in ("source", "target") if relation[field] not in entities
                    ],
                },
            )
        if (
            kind not in RELATION_KINDS
            or len(relation["label"]) > 120
            or (kind == "relation" and not clean(relation["label"]))
        ):
            raise ServiceError("section_relation_fields_invalid")
        if not allowed_relation(kind, source_kind, target_kind):
            raise ServiceError(
                "section_relation_types_invalid",
                details={
                    **location,
                    "relation_kind": kind,
                    "source_kind": source_kind,
                    "target_kind": target_kind,
                },
            )
        evidence(relation, location)

    for index, attribute in enumerate(data["attributes"]):
        location = {"list": "attributes", "index": index}
        record(attribute, "source label type value quote")
        kind, value = attribute["type"], attribute["value"]
        if attribute["source"] not in entities:
            raise ServiceError("section_attribute_entity_unknown", details=location)
        if (
            not clean(attribute["label"])
            or not clean(value)
            or max(len(attribute["label"]), len(value)) > 120
            or kind not in VALUE_KINDS
        ):
            raise ServiceError("section_attribute_fields_invalid")
        try:
            valid_value = (
                re.fullmatch(r"[+-]?[0-9]+", value)
                if kind == "integer"
                else re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", value)
                and Decimal(value).is_finite()
                if kind == "decimal"
                else value in {"true", "false"}
                if kind == "boolean"
                else True
            )
        except InvalidOperation:
            valid_value = False
        if not valid_value:
            raise ServiceError("section_attribute_value_invalid")
        evidence(attribute, location)
    return data


def decode_section(text):
    def unique(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError("duplicate key")
        return value

    try:
        return json.loads(text, object_pairs_hook=unique)
    except ValueError as exc:
        raise ServiceError("section_json_invalid") from exc
