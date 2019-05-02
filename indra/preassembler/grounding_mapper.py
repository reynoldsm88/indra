from __future__ import absolute_import, print_function, unicode_literals
from builtins import dict, str
import os
import csv
import json
import logging
from copy import deepcopy
from collections import Counter
from itertools import groupby, chain
from indra.statements import Agent
from indra.databases import uniprot_client, hgnc_client, chebi_client, \
    mesh_client, go_client
from indra.util import read_unicode_csv, write_unicode_csv

logger = logging.getLogger(__name__)


# If the deft disambiguator is installed, load deft models to
# disambiguate acronyms and shortforms
try:
    from deft import available_shortforms as available_deft_models
    from deft.disambiguate import load_disambiguator
    deft_disambiguators = {}
    for shortform in available_deft_models:
        deft_disambiguators[shortform] = load_disambiguator(shortform)
except Exception:
    logger.info('DEFT will not be available for grounding disambiguation.')
    deft_disambiguators = {}


class GroundingMapper(object):
    """Maps grounding of INDRA Agents based on a given grounding map.

    Parameters
    ----------
    gm : dict
        The grounding map, a dictionary mapping strings (entity names) to
        a dictionary of database identifiers.
    agent_map : Optional[dict]
        A dictionary mapping strings to grounded INDRA Agents with given state.
    use_deft : Optional[bool]
        If True, Deft will be attempted to be used for disambiguation of
        acronyms. Default: True
    """
    def __init__(self, gm, agent_map=None, use_deft=True):
        self.gm = gm
        self.agent_map = agent_map if agent_map is not None else {}
        self.use_deft = use_deft

    def update_agent_db_refs(self, agent, agent_text, do_rename=True):
        """Update db_refs of agent using the grounding map

        If the grounding map is missing one of the HGNC symbol or Uniprot ID,
        attempts to reconstruct one from the other.

        Parameters
        ----------
        agent : :py:class:`indra.statements.Agent`
            The agent whose db_refs will be updated
        agent_text : str
            The agent_text to find a grounding for in the grounding map
            dictionary. Typically this will be agent.db_refs['TEXT'] but
            there may be situations where a different value should be used.
        do_rename: Optional[bool]
            If True, the Agent name is updated based on the mapped grounding.
            If do_rename is True the priority for setting the name is
            FamPlex ID, HGNC symbol, then the gene name
            from Uniprot. Default: True

        Raises
        ------
        ValueError
            If the the grounding map contains and HGNC symbol for
            agent_text but no HGNC ID can be found for it.
        ValueError
            If the grounding map contains both an HGNC symbol and a
            Uniprot ID, but the HGNC symbol and the gene name associated with
            the gene in Uniprot do not match or if there is no associated gene
            name in Uniprot.
        """
        # First we map the Agent's raw text name to get a db_refs dict
        map_db_refs = deepcopy(self.gm.get(agent_text))
        # We then standardize the IDs in the db_refs dict and set it as the
        # Agent's db_refs
        agent.db_refs = self.standardize_db_refs(map_db_refs)
        # Finally, if renaming is needed we standardize the Agent's name
        if do_rename:
            standardize_name(agent)

    @staticmethod
    def standardize_db_refs(db_refs):
        """Return a dict with Harmonized entries from a given db_refs dict.

        Importantly this function assumes that the incoming db_refs dict's
        HGNC entry is an HGNC symbol, whereas the returned db_refs dict's
        HGNC entry is an HGNC ID. Therefore this function should not be used
        for db_refs dicts that don't satisfy this property.
        """
        up_id = db_refs.get('UP')
        hgnc_sym = db_refs.get('HGNC')
        if up_id and not hgnc_sym:
            gene_name = uniprot_client.get_gene_name(up_id, False)
            if gene_name:
                hgnc_id = hgnc_client.get_hgnc_id(gene_name)
                if hgnc_id:
                    db_refs['HGNC'] = hgnc_id
        elif hgnc_sym and not up_id:
            # Override the HGNC symbol entry from the grounding
            # map with an HGNC ID
            hgnc_id = hgnc_client.get_hgnc_id(hgnc_sym)
            if hgnc_id:
                db_refs['HGNC'] = hgnc_id
                # Now get the Uniprot ID for the gene
                up_id = hgnc_client.get_uniprot_id(hgnc_id)
                if up_id:
                    db_refs['UP'] = up_id
            # If there's no HGNC ID for this symbol, raise an
            # Exception
            else:
                raise ValueError('No HGNC ID corresponding to gene '
                                 'symbol %s in grounding map.' %
                                 hgnc_sym)
        # If we have both, check the gene symbol ID against the
        # mapping from Uniprot
        elif up_id and hgnc_sym:
            # Get HGNC Symbol from Uniprot
            gene_name = uniprot_client.get_gene_name(up_id)
            if not gene_name:
                raise ValueError('No gene name found for Uniprot '
                                 'ID %s (expected %s)' %
                                 (up_id, hgnc_sym))
            # We got gene name, compare it to the HGNC name
            else:
                if gene_name != hgnc_sym:
                    raise ValueError('Gene name %s for Uniprot ID '
                                     '%s does not match HGNC '
                                     'symbol %s given in grounding '
                                     'map.' %
                                     (gene_name, up_id, hgnc_sym))
                else:
                    hgnc_id = hgnc_client.get_hgnc_id(hgnc_sym)
                    if not hgnc_id:
                        logger.error('No HGNC ID corresponding to gene '
                                     'symbol %s in grounding map.' % hgnc_sym)
                    else:
                        db_refs['HGNC'] = hgnc_id
        return db_refs

    def map_agents_for_stmt(self, stmt, do_rename=True):
        """Return a new Statement whose agents have been grounding mapped.

        Parameters
        ----------
        stmt : :py:class:`indra.statements.Statement`
            The Statement whose agents need mapping.
        do_rename: Optional[bool]
            If True, the Agent name is updated based on the mapped grounding.
            If do_rename is True the priority for setting the name is
            FamPlex ID, HGNC symbol, then the gene name
            from Uniprot. Default: True

        Returns
        -------
        mapped_stmt : :py:class:`indra.statements.Statement`
            The mapped Statement.
        """
        mapped_stmt = deepcopy(stmt)
        # Iterate over the agents

        # Update agents directly participating in the statement
        agent_list = mapped_stmt.agent_list()
        for idx, agent in enumerate(agent_list):
            if agent is None:
                continue
            agent_txt = agent.db_refs.get('TEXT')
            if agent_txt is None:
                continue

            new_agent, maps_to_none = self.map_agent(agent, do_rename)

            # Check if a deft model exists for agent text
            if self.use_deft and agent_txt in deft_disambiguators:
                try:
                    run_deft_disambiguation(mapped_stmt, agent_list, idx,
                                            new_agent, agent_txt)
                except Exception as e:
                    logger.error('There was an error during Deft'
                                 ' disambiguation.')
                    logger.error(e)

            if maps_to_none:
                # Skip the entire statement if the agent maps to None in the
                # grounding map
                return None

            # If the old agent had bound conditions, but the new agent does
            # not, copy the bound conditions over
            if new_agent is not None and len(new_agent.bound_conditions) == 0:
                new_agent.bound_conditions = agent.bound_conditions

            agent_list[idx] = new_agent

        mapped_stmt.set_agent_list(agent_list)

        # Update agents in the bound conditions
        for agent in agent_list:
            if agent is not None:
                for bc in agent.bound_conditions:
                    bc.agent, maps_to_none = self.map_agent(bc.agent,
                                                            do_rename)
                    if maps_to_none:
                        # Skip the entire statement if the agent maps to None
                        # in the grounding map
                        return None

        return mapped_stmt

    def map_agent(self, agent, do_rename):
        """Return the given Agent with its grounding mapped.

        This function grounds a single agent. It returns the new Agent object
        (which might be a different object if we load a new agent state
        from json) or the same object otherwise.

        Parameters
        ----------
        agent : :py:class:`indra.statements.Agent`
            The Agent to map.
        do_rename: bool
            If True, the Agent name is updated based on the mapped grounding.
            If do_rename is True the priority for setting the name is
            FamPlex ID, HGNC symbol, then the gene name
            from Uniprot.

        Returns
        -------
        grounded_agent : :py:class:`indra.statements.Agent`
            The grounded Agent.
        maps_to_none : bool
            True if the Agent is in the grounding map and maps to None.
        """

        agent_text = agent.db_refs.get('TEXT')
        mapped_to_agent_json = self.agent_map.get(agent_text)
        if mapped_to_agent_json:
            mapped_to_agent = \
                Agent._from_json(mapped_to_agent_json['agent'])
            return mapped_to_agent, False
        # Look this string up in the grounding map
        # If not in the map, leave agent alone and continue
        if agent_text in self.gm.keys():
            map_db_refs = self.gm[agent_text]
        else:
            return agent, False

        # If it's in the map but it maps to None, then filter out
        # this statement by skipping it
        if map_db_refs is None:
            # Increase counter if this statement has not already
            # been skipped via another agent
            logger.debug("Skipping %s" % agent_text)
            return None, True
        # If it has a value that's not None, map it and add it
        else:
            # Otherwise, update the agent's db_refs field
            self.update_agent_db_refs(agent, agent_text, do_rename)
        return agent, False

    def map_agents(self, stmts, do_rename=True):
        """Return a new list of statements whose agents have been mapped

        Parameters
        ----------
        stmts : list of :py:class:`indra.statements.Statement`
            The statements whose agents need mapping
        do_rename: Optional[bool]
            If True, the Agent name is updated based on the mapped grounding.
            If do_rename is True the priority for setting the name is
            FamPlex ID, HGNC symbol, then the gene name
            from Uniprot. Default: True

        Returns
        -------
        mapped_stmts : list of :py:class:`indra.statements.Statement`
            A list of statements given by mapping the agents from each
            statement in the input list
        """
        # Make a copy of the stmts
        mapped_stmts = []
        num_skipped = 0
        # Iterate over the statements
        for stmt in stmts:
            mapped_stmt = self.map_agents_for_stmt(stmt, do_rename)
            # Check if we should skip the statement
            if mapped_stmt is not None:
                mapped_stmts.append(mapped_stmt)
            else:
                num_skipped += 1
        logger.info('%s statements filtered out' % num_skipped)
        return mapped_stmts

    def rename_agents(self, stmts):
        """Return a list of mapped statements with updated agent names.

        Creates a new list of statements without modifying the original list.

        Parameters
        ----------
        stmts : list of :py:class:`indra.statements.Statement`
            List of statements whose Agents need their names updated.

        Returns
        -------
        mapped_stmts : list of :py:class:`indra.statements.Statement`
            A new list of Statements with updated Agent names
        """
        # Make a copy of the stmts
        mapped_stmts = deepcopy(stmts)
        # Iterate over the statements
        for _, stmt in enumerate(mapped_stmts):
            # Iterate over the agents
            for agent in stmt.agent_list():
                standardize_name(agent)
        return mapped_stmts


def standardize_name(agent):
    """Standardize the name of an Agent based on grounding information.

    If an agent contains a FamPlex grounding, the FamPlex ID is used as a
    name. Otherwise if it contains a Uniprot ID, an attempt is made to find
    the associated HGNC gene name. If one can be found it is used as the agent
    name and the associated HGNC ID is added as an entry to the db_refs.
    Similarly, CHEBI, MESH and GO IDs are used in this order of priority to
    assign a standardized name to the Agent. If no relevant IDs are found,
    the name is not changed.

    Parameters
    ----------
    agent : indra.statements.Agent
        An INDRA Agent whose name attribute should be standardized based
        on grounding information.
    """
    # We return immediately for None Agents
    if agent is None:
        return

    # We next look for prioritized grounding, if missing, we return
    db_ns, db_id = agent.get_grounding()
    if not db_ns or not db_id:
        return

    # If there's a FamPlex ID, prefer that for the name
    if db_ns == 'FPLX':
        agent.name = agent.db_refs['FPLX']
    # Take a HGNC name from Uniprot next
    elif db_ns == 'HGNC':
        gene_name = hgnc_client.get_hgnc_name(db_id)
        if gene_name:
            agent.name = gene_name
    elif db_ns == 'UP':
        # Try for the gene name
        gene_name = uniprot_client.get_gene_name(agent.db_refs['UP'],
                                                 web_fallback=False)
        if gene_name:
            agent.name = gene_name
    elif db_ns == 'CHEBI':
        chebi_name = chebi_client.chebi_id_to_name(agent.db_refs['CHEBI'])
        if chebi_name:
            agent.name = chebi_name
    elif db_ns == 'MESH':
        mesh_name = mesh_client.get_mesh_name(agent.db_refs['MESH'])
        if mesh_name:
            agent.name = mesh_name
    elif db_ns == 'GO':
        go_name = go_client.get_go_label(agent.db_refs['GO'])
        if go_name:
            agent.name = go_name
    return


# TODO: handle the cases when there is more than one entry for the same
# key (e.g., ROS, ER)
def load_grounding_map(grounding_map_path, ignore_path=None,
                       lineterminator='\r\n'):
    """Return a grounding map dictionary loaded from a csv file.

    In the file pointed to by grounding_map_path, the number of name_space ID
    pairs can vary per row and commas are
    used to pad out entries containing fewer than the maximum amount of
    name spaces appearing in the file. Lines should be terminated with \r\n
    both a carriage return and a new line by default.

    Optionally, one can specify another csv file (pointed to by ignore_path)
    containing agent texts that are degenerate and should be filtered out.

    Parameters
    ----------
    grounding_map_path : str
        Path to csv file containing grounding map information. Rows of the file
        should be of the form <agent_text>,<name_space_1>,<ID_1>,...
        <name_space_n>,<ID_n>
    ignore_path : Optional[str]
        Path to csv file containing terms that should be filtered out during
        the grounding mapping process. The file Should be of the form
        <agent_text>,,..., where the number of commas that
        appear is the same as in the csv file at grounding_map_path.
        Default: None
    lineterminator : Optional[str]
        Line terminator used in input csv file. Default: \r\n

    Returns
    -------
    g_map : dict
        The grounding map constructed from the given files.
    """
    g_map = {}
    map_rows = read_unicode_csv(grounding_map_path, delimiter=',',
                                quotechar='"',
                                quoting=csv.QUOTE_MINIMAL,
                                lineterminator='\r\n')
    if ignore_path and os.path.exists(ignore_path):
        ignore_rows = read_unicode_csv(ignore_path, delimiter=',',
                                       quotechar='"',
                                       quoting=csv.QUOTE_MINIMAL,
                                       lineterminator=lineterminator)
    else:
        ignore_rows = []
    csv_rows = chain(map_rows, ignore_rows)
    for row in csv_rows:
        key = row[0]
        db_refs = {'TEXT': key}
        keys = [entry for entry in row[1::2] if entry != '']
        values = [entry for entry in row[2::2] if entry != '']
        if len(keys) != len(values):
            logger.info('ERROR: Mismatched keys and values in row %s' %
                        str(row))
            continue
        else:
            db_refs.update(dict(zip(keys, values)))
            if len(db_refs.keys()) > 1:
                g_map[key] = db_refs
            else:
                g_map[key] = None
    return g_map


# Some useful functions for analyzing the grounding of sets of statements
# Put together all agent texts along with their grounding
def all_agents(stmts):
    """Return a list of all of the agents from a list of statements.

    Only agents that are not None and have a TEXT entry are returned.

    Parameters
    ----------
    stmts : list of :py:class:`indra.statements.Statement`

    Returns
    -------
    agents : list of :py:class:`indra.statements.Agent`
        List of agents that appear in the input list of indra statements.
    """
    agents = []
    for stmt in stmts:
        for agent in stmt.agent_list():
            # Agents don't always have a TEXT db_refs entry (for instance
            # in the case of Statements from databases) so we check for this.
            if agent is not None and agent.db_refs.get('TEXT') is not None:
                agents.append(agent)
    return agents


def agent_texts(agents):
    """Return a list of all agent texts from a list of agents.

    None values are associated to agents without agent texts

    Parameters
    ----------
    agents : list of :py:class:`indra.statements.Agent`

    Returns
    -------
    list of str/None
        agent texts from input list of agents
    """
    return [ag.db_refs.get('TEXT') for ag in agents]


def get_sentences_for_agent(text, stmts, max_sentences=None):
    """Returns evidence sentences with a given agent text from a list of statements

    Parameters
    ----------
    text : str
        An agent text

    stmts : list of :py:class:`indra.statements.Statement`
        INDRA Statements to search in for evidence statements.

    max_sentences : Optional[int/None]
        Cap on the number of evidence sentences to return. Default: None

    Returns
    -------
    sentences : list of str
        Evidence sentences from the list of statements containing
        the given agent text.
    """
    sentences = []
    for stmt in stmts:
        for agent in stmt.agent_list():
            if agent is not None and agent.db_refs.get('TEXT') == text:
                sentences.append((stmt.evidence[0].pmid,
                                  stmt.evidence[0].text))
                if max_sentences is not None and \
                   len(sentences) >= max_sentences:
                    return sentences
    return sentences


def agent_texts_with_grounding(stmts):
    """Return agent text groundings in a list of statements with their counts

    Parameters
    ----------
    stmts: list of :py:class:`indra.statements.Statement`

    Returns
    -------
    list of tuple
        List of tuples of the form
        (text: str, ((name_space: str, ID: str, count: int)...),
        total_count: int)

        Where the counts within the tuple of groundings give the number of
        times an agent with the given agent_text appears grounded with the
        particular name space and ID. The total_count gives the total number
        of times an agent with text appears in the list of statements.
    """
    allag = all_agents(stmts)
    # Convert PFAM-DEF lists into tuples so that they are hashable and can
    # be tabulated with a Counter
    for ag in allag:
        pfam_def = ag.db_refs.get('PFAM-DEF')
        if pfam_def is not None:
            ag.db_refs['PFAM-DEF'] = tuple(pfam_def)
    refs = [tuple(ag.db_refs.items()) for ag in allag]
    refs_counter = Counter(refs)
    refs_counter_dict = [(dict(entry[0]), entry[1])
                         for entry in refs_counter.items()]
    # First, sort by text so that we can do a groupby
    refs_counter_dict.sort(key=lambda x: x[0].get('TEXT'))

    # Then group by text
    grouped_by_text = []
    for k, g in groupby(refs_counter_dict, key=lambda x: x[0].get('TEXT')):
        # Total occurrences of this agent text
        total = 0
        entry = [k]
        db_ref_list = []
        for db_refs, count in g:
            # Check if TEXT is our only key, indicating no grounding
            if list(db_refs.keys()) == ['TEXT']:
                db_ref_list.append((None, None, count))
            # Add any other db_refs (not TEXT)
            for db, db_id in db_refs.items():
                if db == 'TEXT':
                    continue
                else:
                    db_ref_list.append((db, db_id, count))
            total += count
        # Sort the db_ref_list by the occurrences of each grounding
        entry.append(tuple(sorted(db_ref_list, key=lambda x: x[2],
                     reverse=True)))
        # Now add the total frequency to the entry
        entry.append(total)
        # And add the entry to the overall list
        grouped_by_text.append(tuple(entry))
    # Sort the list by the total number of occurrences of each unique key
    grouped_by_text.sort(key=lambda x: x[2], reverse=True)
    return grouped_by_text


# List of all ungrounded entities by number of mentions
def ungrounded_texts(stmts):
    """Return a list of all ungrounded entities ordered by number of mentions

    Parameters
    ----------
    stmts : list of :py:class:`indra.statements.Statement`

    Returns
    -------
    ungroundc : list of tuple
       list of tuples of the form (text: str, count: int) sorted in descending
       order by count.
    """
    ungrounded = [ag.db_refs['TEXT']
                  for s in stmts
                  for ag in s.agent_list()
                  if ag is not None and list(ag.db_refs.keys()) == ['TEXT']]
    ungroundc = Counter(ungrounded)
    ungroundc = ungroundc.items()
    ungroundc = sorted(ungroundc, key=lambda x: x[1], reverse=True)
    return ungroundc


def get_agents_with_name(name, stmts):
    """Return all agents within a list of statements with a particular name."""
    return [ag for stmt in stmts for ag in stmt.agent_list()
            if ag is not None and ag.name == name]


def save_base_map(filename, grouped_by_text):
    """Dump a list of agents along with groundings and counts into a csv file

    Parameters
    ----------
    filename : str
        Filepath for output file
    grouped_by_text : list of tuple
        List of tuples of the form output by agent_texts_with_grounding
    """
    rows = []
    for group in grouped_by_text:
        text_string = group[0]
        for db, db_id, count in group[1]:
            if db == 'UP':
                name = uniprot_client.get_mnemonic(db_id)
            else:
                name = ''
            row = [text_string, db, db_id, count, name]
            rows.append(row)

    write_unicode_csv(filename, rows, delimiter=',', quotechar='"',
                      quoting=csv.QUOTE_MINIMAL, lineterminator='\r\n')


def protein_map_from_twg(twg):
    """Build  map of entity texts to validate protein grounding.

    Looks at the grounding of the entity texts extracted from the statements
    and finds proteins where there is grounding to a human protein that maps to
    an HGNC name that is an exact match to the entity text. Returns a dict that
    can be used to update/expand the grounding map.

    Parameters
    ----------
    twg : list of tuple
        list of tuples of the form output by agent_texts_with_grounding

    Returns
    -------
    protein_map : dict
        dict keyed on agent text with associated values
        {'TEXT': agent_text, 'UP': uniprot_id}. Entries are for agent texts
        where the grounding map was able to find human protein grounded to
        this agent_text in Uniprot.
    """

    protein_map = {}
    unmatched = 0
    matched = 0
    logger.info('Building grounding map for human proteins')
    for agent_text, grounding_list, _ in twg:
        # If 'UP' (Uniprot) not one of the grounding entries for this text,
        # then we skip it.
        if 'UP' not in [entry[0] for entry in grounding_list]:
            continue
        # Otherwise, collect all the Uniprot IDs for this protein.
        uniprot_ids = [entry[1] for entry in grounding_list
                       if entry[0] == 'UP']
        # For each Uniprot ID, look up the species
        for uniprot_id in uniprot_ids:
            # If it's not a human protein, skip it
            mnemonic = uniprot_client.get_mnemonic(uniprot_id)
            if mnemonic is None or not mnemonic.endswith('_HUMAN'):
                continue
            # Otherwise, look up the gene name in HGNC and match against the
            # agent text
            gene_name = uniprot_client.get_gene_name(uniprot_id)
            if gene_name is None:
                unmatched += 1
                continue
            if agent_text.upper() == gene_name.upper():
                matched += 1
                protein_map[agent_text] = {'TEXT': agent_text,
                                           'UP': uniprot_id}
            else:
                unmatched += 1
    logger.info('Exact matches for %d proteins' % matched)
    logger.info('No match (or no gene name) for %d proteins' % unmatched)
    return protein_map


def save_sentences(twg, stmts, filename, agent_limit=300):
    """Write evidence sentences for stmts with ungrounded agents to csv file.

    Parameters
    ----------
    twg: list of tuple
        list of tuples of ungrounded agent_texts with counts of the
        number of times they are mentioned in the list of statements.
        Should be sorted in descending order by the counts.
        This is of the form output by the function ungrounded texts.

    stmts: list of :py:class:`indra.statements.Statement`

    filename : str
        Path to output file

    agent_limit : Optional[int]
        Number of agents to include in output file. Takes the top agents
        by count.
    """
    sentences = []
    unmapped_texts = [t[0] for t in twg]
    counter = 0
    logger.info('Getting sentences for top %d unmapped agent texts.' %
                agent_limit)
    for text in unmapped_texts:
        agent_sentences = get_sentences_for_agent(text, stmts)
        sentences += map(lambda tup: (text,) + tup, agent_sentences)
        counter += 1
        if counter >= agent_limit:
            break
    # Write sentences to CSV file
    write_unicode_csv(filename, sentences, delimiter=',', quotechar='"',
                      quoting=csv.QUOTE_MINIMAL, lineterminator='\r\n')


def run_deft_disambiguation(stmt, agent_list, idx, new_agent, agent_txt):
    # Initialize annotations if needed so Deft predicted
    # probabilities can be added to Agent annotations
    annots = stmt.evidence[0].annotations if stmt.evidence else {}
    if 'agents' in annots:
        if 'deft' not in annots['agents']:
            annots['agents']['deft'] = \
                {'deft': [None for _ in agent_list]}
    else:
        annots['agents'] = {'deft': [None for _ in agent_list]}
    grounding_text = _get_text_for_grounding(stmt, agent_txt)
    if grounding_text:
        res = deft_disambiguators[agent_txt].disambiguate(
                                                [grounding_text])
        ns_and_id, standard_name, disamb_scores = res[0]
        # If the highest score is ungrounded we don't do anything
        # TODO: should we explicitly remove grounding if we conclude it
        # doesn't match any of the choices?
        if ns_and_id == 'ungrounded':
            return
        db_ns, db_id = ns_and_id.split(':')
        new_agent.db_refs = {'TEXT': agent_txt, db_ns: db_id}
        new_agent.name = standard_name
        logger.info('Disambiguated %s to: %s, %s:%s' %
                    (agent_txt, standard_name, db_ns, db_id))
        if db_ns == 'HGNC':
            hgnc_sym = hgnc_client.get_hgnc_name(db_id)
            standard_db_refs = \
                GroundingMapper.standardize_db_refs({'HGNC': hgnc_sym})
            new_agent.db_refs = standard_db_refs
        annots['agents']['deft'][idx] = disamb_scores


def _get_text_for_grounding(stmt, agent_text):
    """Get text context for Deft disambiguation

    If the INDRA database is available, attempts to get the fulltext from
    which the statement was extracted. If the fulltext is not available, the
    abstract is returned. If the indra database is not available, uses the
    pubmed client to get the abstract. If no abstract can be found, falls back
    on returning the evidence text for the statement.

    Parameters
    ----------
    stmt : py:class:`indra.statements.Statement`
        Statement with agent we seek to disambiguate.

    agent_text : str
       Agent text that needs to be disambiguated

    Returns
    -------
    text : str
        Text for Feft disambiguation
    """
    text = None
    # First we will try to get content from the DB
    try:
        from indra_db.util.content_scripts \
            import get_text_content_from_text_refs
        from indra.literature.deft_tools import universal_extract_text
        refs = stmt.evidence[0].text_refs
        # Prioritize the pmid attribute if given
        if stmt.evidence[0].pmid:
            refs['PMID'] = stmt.evidence[0].pmid
        logger.info('Obtaining text for disambiguation with refs: %s' %
                    refs)
        content = get_text_content_from_text_refs(refs)
        text = universal_extract_text(content, contains=agent_text)
        if text:
            return text
    except Exception as e:
        logger.info('Could not get text for disambiguation from DB.')
    # If that doesn't work, we try PubMed next
    if text is None:
        from indra.literature import pubmed_client
        pmid = stmt.evidence[0].pmid
        if pmid:
            logger.info('Obtaining abstract for disambiguation for PMID%s' %
                        pmid)
            text = pubmed_client.get_abstract(pmid)
            if text:
                return text
    # Finally, falling back on the evidence sentence
    if text is None:
        logger.info('Falling back on sentence-based disambiguation')
        text = stmt.evidence[0].text
        return text
    return None


default_grounding_map_path = \
    os.path.join(os.path.dirname(__file__),
                 '../resources/famplex/grounding_map.csv')
default_ignore_path = \
    os.path.join(os.path.dirname(__file__),
                 '../resources/famplex/ignore.csv')
default_agent_grounding_path = \
    os.path.join(os.path.dirname(__file__),
                 '../resources/grounding_agents.json')
default_grounding_map = \
    load_grounding_map(default_grounding_map_path, default_ignore_path)


gm = default_grounding_map
with open(default_agent_grounding_path, 'r') as fh:
    default_agent_map = json.load(fh)
