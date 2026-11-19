"""Deterministic reconciliation and RDF/XML generation from grounded records."""

from rdflib import OWL, RDF, RDFS, XSD, BNode, Graph, Literal, Namespace, URIRef

from .contracts import ServiceError, canonical, sha256
from .section_schema import PREDICATES, clean, identity

VALUE_TYPES = {
    'integer': XSD.integer, 'decimal': XSD.decimal, 'boolean': XSD.boolean,
}


def compile_owl(iri, sections):
    graph = Graph()
    ns = Namespace(iri + '#')
    for name, namespace in [('owl', OWL), ('rdf', RDF), ('rdfs', RDFS), ('pc', ns), ('xsd', XSD)]:
        graph.bind(name, namespace)
    graph.add((URIRef(iri), RDF.type, OWL.Ontology))
    graph.add((ns.fonte, RDF.type, OWL.AnnotationProperty))
    graph.add((ns.ambito, RDF.type, OWL.AnnotationProperty))
    entities = set()
    operational = set()

    def digest(*parts):
        return sha256(canonical(parts))

    def annotate(triple, section, quote):
        graph.add(triple)
        axiom = BNode('b_' + digest('axiom', *(term.n3() for term in triple)))
        graph.add((axiom, RDF.type, OWL.Axiom))
        for prop, value in zip((OWL.annotatedSource, OWL.annotatedProperty, OWL.annotatedTarget),
                               triple, strict=True):
            graph.add((axiom, prop, value))
        graph.add((axiom, ns.fonte, Literal(
            f"doc={section['document_id']}; page={section['page']}; quote={clean(quote)}"
        )))

    for section, data in sections:
        local = {}
        for row in data['entities']:
            kind, label, scope = row['kind'], clean(row['label']), clean(row['scope'])
            entity = ns['e_' + digest(kind, identity(label), identity(scope))]
            local[row['id']] = (entity, kind)
            if entity not in entities:
                entities.add(entity)
                rdf_kind = OWL.Class if kind == 'class' else OWL.NamedIndividual
                graph.add((entity, RDF.type, rdf_kind))
                graph.add((entity, RDFS.label, Literal(label)))
                if kind == 'procedure':
                    graph.add((ns.ProceduraDocumentata, RDF.type, OWL.Class))
                    graph.add((entity, RDF.type, ns.ProceduraDocumentata))
                if scope:
                    graph.add((entity, ns.ambito, Literal(scope)))

        for row in data['relations']:
            source, source_kind = local[row['source']]
            target, _ = local[row['target']]
            kind = row['kind']
            if kind == 'subclass':
                if source != target:
                    annotate((source, RDFS.subClassOf, target), section, row['quote'])
                continue
            if kind == 'type':
                annotate((source, RDF.type, target), section, row['quote'])
                continue
            label = clean(row['label']) if kind == 'relation' else PREDICATES[kind]
            prop = ns['r_' + digest(identity(label))] if kind == 'relation' else ns[label]
            graph.add((prop, RDF.type, OWL.ObjectProperty))
            graph.add((prop, RDFS.label, Literal(label)))
            if source_kind == 'class':
                restriction = BNode('b_' + digest('restriction', str(source), str(prop), str(target)))
                graph.add((restriction, RDF.type, OWL.Restriction))
                graph.add((restriction, OWL.onProperty, prop))
                graph.add((restriction, OWL.someValuesFrom, target))
                triple = (source, RDFS.subClassOf, restriction)
            else:
                triple = (source, prop, target)
            annotate(triple, section, row['quote'])
            if kind in PREDICATES:
                operational.add(triple)

        for row in data['attributes']:
            source, _ = local[row['source']]
            label = clean(row['label'])
            prop = ns['a_' + digest(identity(label))]
            graph.add((prop, RDF.type, OWL.DatatypeProperty))
            graph.add((prop, RDFS.label, Literal(label)))
            value = Literal(clean(row['value']), datatype=VALUE_TYPES.get(row['type']))
            annotate((source, prop, value), section, row['quote'])

    if not entities:
        raise ServiceError('ontology_no_entities')
    if not any(graph.subjects(RDF.type, OWL.Class)):
        category = ns.EntitaDocumentata
        graph.add((category, RDF.type, OWL.Class))
        for entity in entities:
            graph.add((entity, RDF.type, category))
    return graph.serialize(format='xml'), {
        'operational_relations': len(operational),
        'reconciled_entities': len(entities),
    }
