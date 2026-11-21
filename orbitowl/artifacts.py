"""Mechanical RDF/XML normalization only. Logical and semantic validation are separate."""
import io
import re
from xml.etree import ElementTree as ET

from rdflib import OWL, RDF, RDFS, Graph, URIRef

from .contracts import ServiceError

MAX_ARTIFACT_BYTES = 64 * 1024 * 1024


def repair_mixed_subclass(text, root, namespaces):
    """Translate only the unambiguous two-named-class hybrid; preserve operand order."""
    hybrids = root.findall('{'+str(OWL)+'}SubClassOf')
    if not hybrids:
        return text, 0
    if any(namespaces.get(prefix) != str(uri) for prefix, uri in
           [('owl', OWL), ('rdf', RDF), ('rdfs', RDFS)]):
        raise ServiceError('unsupported_mixed_owlxml')
    about = '{'+str(RDF)+'}about'
    for axiom in hybrids:
        if (axiom.attrib or (axiom.text or '').strip() or len(axiom) != 2
                or any(child.tag != '{'+str(OWL)+'}Class' or set(child.attrib) != {about}
                       or not child.attrib[about] or len(child) or (child.text or '').strip()
                       or (child.tail or '').strip() for child in axiom)):
            raise ServiceError('unsupported_mixed_owlxml')
    pattern = re.compile(
        r'''<owl:SubClassOf\s*>\s*<owl:Class\s+rdf:about=(?P<q1>["'])(?P<sub>[^"']+)(?P=q1)\s*/>'''
        r'''\s*<owl:Class\s+rdf:about=(?P<q2>["'])(?P<sup>[^"']+)(?P=q2)\s*/>\s*</owl:SubClassOf\s*>''')
    if len(list(pattern.finditer(text))) != len(hybrids):
        # Comments, nested constructs or richer serializations need explicit handling.
        raise ServiceError('unsupported_mixed_owlxml')

    def replacement(match):
        sub = match['q1']+match['sub']+match['q1']
        sup = match['q2']+match['sup']+match['q2']
        # Retain both typed class nodes, not just an untyped superclass reference.
        return ('<owl:Class rdf:about='+sub+'><rdfs:subClassOf>'
                '<owl:Class rdf:about='+sup+'/></rdfs:subClassOf></owl:Class>')

    return pattern.sub(replacement, text), len(hybrids)


def normalize(text, expected_iri=None, *, strict=False):
    if len(text.encode()) > MAX_ARTIFACT_BYTES:
        raise ServiceError('artifact_too_large')
    if '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper():
        raise ServiceError('xml_entities_forbidden')
    changes = {}
    if not strict:
        text = text.strip()
        if text.startswith('```') and text.endswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        bad = 'http://www.w3.org/1999/02/22-0-rdf-syntax-ns#'
        changes['rdf_namespace'] = text.count(bad)
        text = text.replace(bad, str(RDF))
    try:
        namespaces = {prefix: uri for _, (prefix, uri) in ET.iterparse(io.StringIO(text), events=['start-ns'])}
        root = ET.fromstring(text)
        if root.tag != '{'+str(RDF)+'}RDF':
            raise ValueError
    except (ET.ParseError, ValueError) as exc:
        raise ServiceError('invalid_xml') from exc
    if not strict:
        text, changes['mixed_subclass_axioms'] = repair_mixed_subclass(text, root, namespaces)
        for prefix in ['pc', 'owl', 'rdfs', 'xsd', 'rdf']:
            if prefix in namespaces:
                pattern = r'''((?:rdf:about|rdf:resource|rdf:datatype)\s*=\s*)(["'])'''+prefix+r''':([^"'\s]+)\2'''
                text, count = re.subn(pattern, lambda m: m[1]+m[2]+namespaces[prefix]+m[3]+m[2], text)
                changes['iri_'+prefix] = count
        for tag in ['annotatedSource', 'annotatedProperty', 'annotatedTarget']:
            text, count = re.subn(r'<(/?)'+tag+r'(?=[\s/>])', r'<\1owl:'+tag, text)
            changes[tag] = count
    try:
        graph = Graph().parse(data=text, format='xml', publicID=expected_iri)
    except Exception as exc:
        raise ServiceError('invalid_rdfxml') from exc
    if any(graph.triples((None, OWL.imports, None))):
        raise ServiceError('ontology_imports_forbidden')
    ontologies = set(graph.subjects(RDF.type, OWL.Ontology))
    classes = {c for c in graph.subjects(RDF.type, OWL.Class) if isinstance(c, URIRef)}
    if not ontologies or not classes:
        raise ServiceError('empty_ontology')
    if expected_iri and ontologies != {URIRef(expected_iri)}:
        raise ServiceError('ontology_iri_mismatch')
    for triple in graph:
        if any(isinstance(t, URIRef) and str(t).startswith(('file:', 'pc:', 'owl:', 'rdfs:', 'xsd:')) for t in triple):
            raise ServiceError('invalid_ontology_iri')
    missing_targets = 0
    for axiom in graph.subjects(RDF.type, OWL.Axiom):
        target = tuple(graph.value(axiom, predicate) for predicate in
                       (OWL.annotatedSource, OWL.annotatedProperty, OWL.annotatedTarget))
        if any(value is None for value in target) or target not in graph:
            missing_targets += 1
    return text, {'normalization': changes, 'triples': len(graph), 'classes': len(classes),
        'object_properties': len(set(graph.subjects(RDF.type, OWL.ObjectProperty))),
        'datatype_properties': len(set(graph.subjects(RDF.type, OWL.DatatypeProperty))),
        'restrictions': len(set(graph.subjects(RDF.type, OWL.Restriction))),
        'unasserted_axiom_annotations': missing_targets,
        'logical_check': 'not_run', 'semantic_review': 'not_performed'}
