"""RDF/XML profile for direct OWL generation with llama.cpp GBNF."""
import json
import re

NAMESPACES = {
    'rdf': 'http://www.w3.org/1999/02/22-rdf-syntax-ns#',
    'rdfs': 'http://www.w3.org/2000/01/rdf-schema#',
    'owl': 'http://www.w3.org/2002/07/owl#',
    'xsd': 'http://www.w3.org/2001/XMLSchema#',
}

NODES = {
    'owl:Class', 'owl:ObjectProperty', 'owl:DatatypeProperty', 'owl:AnnotationProperty',
    'owl:NamedIndividual', 'owl:Restriction', 'owl:Axiom', 'owl:AllDisjointClasses',
    'owl:AllDisjointProperties', 'owl:AllDifferent', 'owl:NegativePropertyAssertion',
    'rdfs:Datatype', 'rdf:Description', 'rdf:List',
}
RELATIONS = {
    'rdf:type', 'rdf:first', 'rdf:rest', 'rdfs:subClassOf', 'rdfs:subPropertyOf',
    'rdfs:domain', 'rdfs:range', 'rdfs:seeAlso', 'rdfs:isDefinedBy',
    'owl:annotatedSource', 'owl:annotatedProperty', 'owl:annotatedTarget',
    'owl:onProperty', 'owl:onDatatype', 'owl:onClass', 'owl:onDataRange',
    'owl:someValuesFrom', 'owl:allValuesFrom', 'owl:hasValue', 'owl:hasSelf',
    'owl:equivalentClass', 'owl:equivalentProperty', 'owl:inverseOf',
    'owl:disjointWith', 'owl:propertyDisjointWith', 'owl:complementOf',
    'owl:datatypeComplementOf', 'owl:sameAs', 'owl:differentFrom',
    'owl:sourceIndividual', 'owl:assertionProperty', 'owl:targetIndividual',
    'owl:targetValue', 'owl:versionIRI', 'owl:priorVersion',
}
COLLECTIONS = {
    'owl:intersectionOf', 'owl:unionOf', 'owl:oneOf', 'owl:disjointUnionOf',
    'owl:members', 'owl:distinctMembers', 'owl:propertyChainAxiom', 'owl:hasKey',
    'owl:withRestrictions',
}
LITERALS = {
    'rdfs:label', 'rdfs:comment', 'owl:versionInfo', 'owl:deprecated',
    'owl:cardinality', 'owl:minCardinality', 'owl:maxCardinality',
    'owl:qualifiedCardinality', 'owl:minQualifiedCardinality', 'owl:maxQualifiedCardinality',
    'pc:fonte', 'pc:livello', 'xsd:minInclusive', 'xsd:maxInclusive',
    'xsd:minExclusive', 'xsd:maxExclusive', 'xsd:length', 'xsd:minLength',
    'xsd:maxLength', 'xsd:pattern', 'xsd:totalDigits', 'xsd:fractionDigits',
}


def owl_grammar(iri):
    """Bind this grammar to the expected ontology IRI and known namespaces."""
    if not re.fullmatch(r'urn:[a-zA-Z0-9:._-]+', iri):
        raise ValueError('Unsupported ontology IRI')
    terminal = json.dumps
    ns = {**NAMESPACES, 'pc': iri + '#'}
    root_attributes = [f'xmlns:{name}="{value}"' for name, value in ns.items()]
    root_attributes.append(f'xml:base="{iri}"')
    rules = [
        'root ::= reasoning "</think>" ws declaration? document ws',
        'document ::= "<rdf:RDF" ' + ' '.join('space ' + terminal(attr) for attr in root_attributes)
        + ' ws ">" ws ontology ws (other-node ws)* owl-Class ws (node ws)* "</rdf:RDF>"',
        'ontology ::= ' + terminal(f'<owl:Ontology rdf:about="{iri}"')
        + ' ws ("/>" | ">" ws (property ws)* "</owl:Ontology>")',
        'other-node ::= ' + ' | '.join(name.replace(':', '-') for name in sorted(NODES - {'owl:Class'})),
        'node ::= other-node | owl-Class',
        'property ::= ' + ' | '.join(name.replace(':', '-') for name in sorted(RELATIONS | COLLECTIONS | LITERALS))
        + ' | custom-resource',
    ]
    for tag in sorted(NODES | RELATIONS | COLLECTIONS | LITERALS):
        opening, closing = terminal('<' + tag), terminal('</' + tag + '>')
        if tag in NODES:
            form = 'node-id? ws ("/>" | ">" ws (property ws)* ' + closing + ')'
        elif tag in COLLECTIONS:
            form = '(resource ws "/>" | collection ws ">" ws (node ws)* ' + closing
            form += ' | ws ">" ws node ws ' + closing + ')'
        else:
            forms = ['literal-meta? ws ">" text ' + closing]
            if tag in RELATIONS:
                forms = ['resource ws "/>"', 'ws ">" ws node ws ' + closing]
                if tag in {'owl:annotatedTarget', 'owl:hasValue', 'owl:hasSelf', 'owl:targetValue', 'rdf:first'}:
                    forms.append('literal-meta? ws ">" text ' + closing)
            form = '(' + ' | '.join(forms) + ')'
        rules.append(f'{tag.replace(":", "-")} ::= {opening} {form}')
    # Disjoint transitions consume each possible mismatch with </think>.
    # This keeps the grammar inactive on XML constructs during the thinking block.
    marker = '</think>'
    mismatches = [
        terminal(marker[1:i]) + ' [^<' + ch + ']'
        for i, ch in enumerate(marker) if i > 1
    ]
    rules.append('reasoning ::= ([^<] | "<"+ (' + ' | '.join(['[^</]', *mismatches]) + '))*')
    rules.extend(r'''
declaration ::= "<?xml version=\"1.0\" encoding=\"UTF-8\"?>" ws
ws ::= [ \t\r\n]*
space ::= [ \t\r\n]+
name ::= [A-Za-z_] [A-Za-z0-9_.-]*
node-id ::= space ("rdf:about=\"" iri "\"" | "rdf:nodeID=\"" name "\"")
resource ::= space ("rdf:resource=\"" iri "\"" | "rdf:nodeID=\"" name "\"")
collection ::= space "rdf:parseType=\"Collection\""
literal-meta ::= space ("xml:lang=\"" [A-Za-z]+ ("-" [A-Za-z0-9]+)* "\"" | "rdf:datatype=\"" iri "\"")
iri ::= ("http://" | "https://" | "urn:") ([a-zA-Z0-9_~:/?#@!$'()*+,;=.-] | "%" [0-9a-fA-F] [0-9a-fA-F] | "&amp;")+
custom-resource ::= "<pc:" name resource ws "/>"
text ::= ([\x09\x0a\x0d\x20-\x25\x27-\x3b\x3d\x3f-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff] | entity)*
entity ::= "&amp;" | "&lt;" | "&gt;" | "&apos;" | "&quot;"
'''.strip().splitlines())
    return '\n'.join(rules) + '\n'
