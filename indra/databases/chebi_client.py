import os
import logging
import requests
from lxml import etree
from functools import lru_cache, cmp_to_key
from indra.util import read_unicode_csv


logger = logging.getLogger(__name__)

# Namespaces used in the XML
chebi_xml_ns = {'n': 'http://schemas.xmlsoap.org/soap/envelope/',
                'c': 'https://www.ebi.ac.uk/webservices/chebi'}


def _strip_prefix(chid):
    if chid and chid.startswith('CHEBI:'):
        return chid[6:]
    else:
        return chid


def get_pubchem_id(chebi_id):
    """Return the PubChem ID corresponding to a given ChEBI ID.

    Parameters
    ----------
    chebi_id : str
        ChEBI ID to be converted.

    Returns
    -------
    pubchem_id : str
        PubChem ID corresponding to the given ChEBI ID. If the lookup fails,
        None is returned.
    """
    pubchem_id = chebi_pubchem.get(_strip_prefix(chebi_id))
    return pubchem_id


def get_chebi_id_from_pubchem(pubchem_id):
    """Return the ChEBI ID corresponding to a given Pubchem ID.

    Parameters
    ----------
    pubchem_id : str
        Pubchem ID to be converted.

    Returns
    -------
    chebi_id : str
        ChEBI ID corresponding to the given Pubchem ID. If the lookup fails,
        None is returned.
    """
    chebi_id = pubchem_chebi.get(pubchem_id)
    return chebi_id


def get_chembl_id(chebi_id):
    """Return a ChEMBL ID from a given ChEBI ID.

    Parameters
    ----------
    chebi_id : str
        ChEBI ID to be converted.

    Returns
    -------
    chembl_id : str
        ChEMBL ID corresponding to the given ChEBI ID. If the lookup fails,
        None is returned.
    """
    return chebi_chembl.get(_strip_prefix(chebi_id))


def get_chebi_id_from_cas(cas_id):
    """Return a ChEBI ID corresponding to the given CAS ID.

    Parameters
    ----------
    cas_id : str
        The CAS ID to be converted.

    Returns
    -------
    chebi_id : str
        The ChEBI ID corresponding to the given CAS ID. If the lookup
        fails, None is returned.
    """
    return cas_chebi.get(cas_id)


def get_chebi_name_from_id(chebi_id, offline=False):
    """Return a ChEBI name corresponding to the given ChEBI ID.

    Parameters
    ----------
    chebi_id : str
        The ChEBI ID whose name is to be returned.
    offline : Optional[bool]
        Choose whether to allow an online lookup if the local lookup fails. If
        True, the online lookup is not attempted. Default: False.

    Returns
    -------
    chebi_name : str
        The name corresponding to the given ChEBI ID. If the lookup
        fails, None is returned.
    """
    chebi_name = chebi_id_to_name.get(_strip_prefix(chebi_id))
    if chebi_name is None and not offline:
        chebi_name = get_chebi_name_from_id_web(_strip_prefix(chebi_id))
    return chebi_name


def get_chebi_id_from_name(chebi_name):
    """Return a ChEBI ID corresponding to the given ChEBI name.

    Parameters
    ----------
    chebi_name : str
        The ChEBI name whose ID is to be returned.

    Returns
    -------
    chebi_id : str
        The ID corresponding to the given ChEBI name. If the lookup
        fails, None is returned.
    """
    chebi_id = chebi_name_to_id.get(chebi_name)
    return chebi_id


@lru_cache(maxsize=5000)
def get_chebi_entry_from_web(chebi_id):
    """Return a ChEBI entry corresponding to a given ChEBI ID using a REST API.

    Parameters
    ----------
    chebi_id : str
        The ChEBI ID whose entry is to be returned.

    Returns
    -------
    xml.etree.ElementTree.Element
        An ElementTree element representing the ChEBI entry.
    """
    url_base = 'http://www.ebi.ac.uk/webservices/chebi/2.0/test/'
    url_fmt = url_base + 'getCompleteEntity?chebiId=%s'
    resp = requests.get(url_fmt % chebi_id)
    if resp.status_code != 200:
        logger.warning("Got bad code form CHEBI client: %s" % resp.status_code)
        return None
    tree = etree.fromstring(resp.content)
    path = 'n:Body/c:getCompleteEntityResponse/c:return'
    elem = tree.find(path, namespaces=chebi_xml_ns)
    return elem


def _get_chebi_value_from_entry(entry, key):
    if entry is None:
        return None
    path = 'c:%s' % key
    elem = entry.find(path, namespaces=chebi_xml_ns)
    if elem is not None:
        return elem.text
    return None


def get_chebi_name_from_id_web(chebi_id):
    """Return a ChEBI name corresponding to a given ChEBI ID using a REST API.

    Parameters
    ----------
    chebi_id : str
        The ChEBI ID whose name is to be returned.

    Returns
    -------
    chebi_name : str
        The name corresponding to the given ChEBI ID. If the lookup
        fails, None is returned.
    """
    entry = get_chebi_entry_from_web(chebi_id)
    return _get_chebi_value_from_entry(entry, 'chebiAsciiName')


def get_inchi_key(chebi_id):
    """Return an InChIKey corresponding to a given ChEBI ID using a REST API.

    Parameters
    ----------
    chebi_id : str
        The ChEBI ID whose InChIKey is to be returned.

    Returns
    -------
    str
        The InChIKey corresponding to the given ChEBI ID. If the lookup
        fails, None is returned.
    """
    entry = get_chebi_entry_from_web(chebi_id)
    return _get_chebi_value_from_entry(entry, 'inchiKey')


def get_primary_id(chebi_id):
    pid = chebi_to_primary.get(chebi_id)
    if pid:
        return pid
    elif chebi_id in chebi_id_to_name:
        return chebi_id
    else:
        return None


def get_specific_id(chebi_ids):
    """Return the most specific ID in a list based on the hierarchy.

    Parameters
    ----------
    chebi_ids : list of str
        A list of ChEBI IDs some of which may be hierarchically related.

    Returns
    -------
    str
        The first ChEBI ID which is at the most specific level in the
        hierarchy with respect to the input list.
    """
    if not chebi_ids:
        return chebi_ids

    from indra.preassembler.hierarchy_manager import hierarchies

    def isa_cmp(a, b):
        """Compare two entries based on isa relationships for sorting."""
        if not a.startswith('CHEBI:'):
            a = 'CHEBI:%s' % a
        if not b.startswith('CHEBI:'):
            b = 'CHEBI:%s' % b
        eh = hierarchies['entity']
        if eh.isa('CHEBI', a, 'CHEBI', b):
            return -1
        if eh.isa('CHEBI', b, 'CHEBI', a):
            return 1
        return 0

    chebi_id = sorted(chebi_ids, key=cmp_to_key(isa_cmp))[0]
    return chebi_id


# Read resource files into module-level variables

def _read_chebi_to_pubchem():
    csv_reader = _read_resource_csv('chebi_to_pubchem.tsv')
    chebi_pubchem = {}
    pubchem_chebi = {}
    ik_matches = {}
    # Here, in case there are many possible mappings, we make it so that we
    # end up with one that has an explicit InChiKey match over one that
    # doesn't, if such a mapping is available
    for chebi_id, pc_id, ik_match in csv_reader:
        if chebi_id not in chebi_pubchem:
            chebi_pubchem[chebi_id] = pc_id
            ik_matches[(chebi_id, pc_id)] = ik_match
        elif ik_match == 'Y' and not \
                ik_matches.get((chebi_id, chebi_pubchem[chebi_id])):
            chebi_pubchem[chebi_id] = pc_id
        if pc_id not in pubchem_chebi:
            pubchem_chebi[pc_id] = chebi_id
            ik_matches[(chebi_id, pc_id)] = ik_match
        elif ik_match == 'Y' and not \
                ik_matches.get((pubchem_chebi[pc_id], pc_id)):
            pubchem_chebi[pc_id] = chebi_id
    return chebi_pubchem, pubchem_chebi


def _read_chebi_to_chembl():
    csv_reader = _read_resource_csv('chebi_to_chembl.tsv')
    chebi_chembl = {}
    for row in csv_reader:
        chebi_chembl[row[0]] = row[1]
    return chebi_chembl


def _read_cas_to_chebi():
    csv_reader = _read_resource_csv('cas_to_chebi.tsv')
    cas_chebi = {}
    next(csv_reader)
    for row in csv_reader:
        cas_chebi[row[0]] = row[1]
    # These are missing from the resource but appear often, so we map
    # them manually
    extra_entries = {'24696-26-2': '17761',
                     '23261-20-3': '18035',
                     '165689-82-7': '16618'}
    cas_chebi.update(extra_entries)
    return cas_chebi


def _read_chebi_names():
    csv_reader = _read_resource_csv('chebi_entries.tsv')
    next(csv_reader)
    chebi_id_to_name = {}
    chebi_name_to_id = {}
    chebi_to_primary = {}
    for row in csv_reader:
        chebi_id, name, secondaries = row
        chebi_id_to_name[chebi_id] = name
        chebi_name_to_id[name] = chebi_id
        for secondary_id in secondaries.split(','):
            chebi_to_primary[secondary_id] = chebi_id
    return chebi_id_to_name, chebi_name_to_id, chebi_to_primary


def _read_hmsb_to_chebi():
    csv_reader = _read_resource_csv('hmdb_to_chebi.tsv')
    hmdb_chebi = {}
    next(csv_reader)
    for row in csv_reader:
        hmdb_chebi[row[0]] = row[1]
    return hmdb_chebi


def _read_resource_csv(fname):
    file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             os.pardir, 'resources', fname)
    csv_reader = read_unicode_csv(file_path, delimiter='\t')
    return csv_reader


chebi_id_to_name, chebi_name_to_id, chebi_to_primary = _read_chebi_names()
chebi_pubchem, pubchem_chebi = _read_chebi_to_pubchem()
chebi_chembl = _read_chebi_to_chembl()
cas_chebi = _read_cas_to_chebi()
hmdb_chebi = _read_hmdb_to_chebi()
