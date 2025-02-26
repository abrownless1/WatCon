"""
Generate water networks based on dynamical data
"""

import os, sys
import numpy as np
import MDAnalysis as mda
from MDAnalysis.analysis import distances
from joblib import Parallel, delayed  # For parallel processing
import networkx as nx
import matplotlib.pyplot as plt

from scipy.spatial import cKDTree
from scipy.spatial.distance import pdist, squareform
import numpy as np

from WatCon.sequence_processing import *
import WatCon.sequence_processing as sequence_processing
from WatCon.visualize_structures import project_clusters
import WatCon.residue_analysis as residue_analysis

class WaterAtom:
    def __init__(self, index, atom_name, residue_number, x, y, z):
        self.index = index
        self.coordinates = (x, y, z)
        self.resname = 'WAT'
        self.name = atom_name
        self.resid = residue_number

class WaterMolecule:
    def __init__(self, index, O: WaterAtom, H1: WaterAtom, H2: WaterAtom, residue_number):
        self.index = index
        self.H1 = H1
        self.H2 = H2
        self.O = O
        self.resname = 'WAT'
        self.resid = residue_number

class OtherAtom:
    def __init__(self, index, atom_name, residue_name, x, y, z, residue_number, msa_residue_number, hbonding):
        self.index = index
        self.coordinates = (x, y, z)
        self.resname = residue_name
        self.msa_resid = msa_residue_number
        self.resid = residue_number
        self.name = atom_name
        self.hbonding = hbonding
  
class WaterNetwork:  #For water-protein analysis -- extrapolate to other solvent maybe
    def __init__(self):
        self.water_molecules = []
        self.protein_atoms = []
        self.protein_subset = []
        self.molecules = []
        self.connections = None
        self.active_site = None
        self.graph = None

    def add_atom(self, index, atom_name, residue_name, x, y, z, residue_number=None, msa_residue_number=None):
        """
        Add protein/misc atom

        Returns:
        None
        """
        if residue_name == 'WAT' or residue_name == 'HOH' or residue_name == 'SOL': #these names are hardcoded
            water  = WaterMolecule(index, atom_name, x, y, z) 
            self.water_molecules.append(water)
        else: #Currently only adds protein atoms
            if ('O' in atom_name) or ('N' in atom_name) or ('S' in atom_name) or ('P' in atom_name):
                hbonding = True
            else:
                hbonding = False

            mol = OtherAtom(index, atom_name, residue_name, x, y, z, residue_number, msa_residue_number, hbonding)
            self.protein_atoms.append(mol)

    def add_water(self, index, o, h1, h2, residue_number):
        """
        Add water molecule

        Returns: 
        None
        """
        o = WaterAtom(o.index, 'O', residue_number, *o.position)
        h1 = WaterAtom(h1.index, 'H1',residue_number, *h1.position)
        h2 = WaterAtom(h2.index, 'H2',residue_number, *h2.position)
        water = WaterMolecule(index, o, h1, h2, residue_number)
        self.water_molecules.append(water)

    def select_active_site(self, reference, box, active_site_radius=8.0):
        """
        Selects active site atoms based on distance to reference atoms.

        Parameters:
        - reference: AtomGroup (or list of atoms) defining the reference
        - box: Simulation box (for PBC)
        - dist_cutoff: default 8.0, Distance cutoff for selection

        Returns:
        - self.active_site
        - active_site protein atoms
        - active_site water atoms
        """
        #Create empty set for active site atoms
        active_site_atoms = []

        #Find coordinates for refrence point
        reference_positions = np.array([ref.position for ref in reference])  # Precompute reference positions

        #Find protein atoms in active site
        protein_active = []

        reference_resids = {r.resid for r in reference}  # Set of reference resids for fast lookup

        for atm in self.protein_atoms:
            #Immediately include atoms which are a part of the reference
            if atm.resid in reference_resids:
                active_site_atoms.append(atm)

            #Include atoms within a distance cutoff
            else:
                dist = np.min(distances.distance_array(np.array(atm.coordinates).reshape(1, -1), reference_positions, box=box))
                if dist <= active_site_radius:
                    active_site_atoms.append(atm)
                    protein_active.append(atm)

        #Find water molecules in active site
        water_active = []

        for mol in self.water_molecules:
            #Include atoms within a distance cutoff
            water_positions = np.array([mol.O.coordinates, mol.H1.coordinates, mol.H2.coordinates])
            
            dist = np.min(distances.distance_array(water_positions, reference_positions))
            if dist <= active_site_radius:            
                active_site_atoms.append(mol)
                water_active.append(mol)


        self.active_site = list(active_site_atoms)  # Convert set back to list if order matters
        return self.active_site, list(protein_active), list(water_active)

    def find_connections(self, dist_cutoff=3.3, water_active=None, protein_active=None, active_site_only=False, water_only=False):
        """
        Find shortest connections using a K-D Tree.

        Parameters:
        - dist_cutoff: Distance cutoff (default 3.3)
        - water_active: Active site water molecule selection
        - protein_active: Active site protein molecule selection
        - active_site_only: Only find connections among active site atoms
        - water_only: Only find connections among waters

        Returns:
        List of connections where:
        - connections[0] = index1
        - connections[1] = index2
        - connections[2] = atom name (of index1)
        - connections[3] = 'WAT-WAT' or 'WAT-PROT'
        - connections[4] = 'active_site' or 'not_active_site'
        """

        connections = []

        # Select active site atoms if specified
        if active_site_only:
            waters = water_active
            protein = protein_active
        else:
            waters = self.water_molecules
            protein = self.protein_atoms

        # Extract water oxygen coordinates and indices
        water_coords = np.array([mol.O.coordinates for mol in waters])
        water_indices = np.array([mol.O.index for mol in waters])
        water_names = np.array(['O' for _ in waters])
        
        # Water-Water connections
        tree = cKDTree(water_coords)
        dist, indices = tree.query(water_coords, k=10, distance_upper_bound=dist_cutoff)

        for i, neighbors in enumerate(indices):
            for j, neighbor in enumerate(neighbors):
                if neighbor != i and dist[i, j] <= dist_cutoff:
                    site_status = (
                        'None' if self.active_site is None else
                        'active_site' if any(waters[i].resid == f.resid for f in self.active_site) else
                        'not_active_site'
                    )
                    if water_indices[i] < water_indices[neighbor]:
                        connections.append((water_indices[i], water_indices[neighbor], water_names[i], 'WAT-WAT', site_status))

        # Water-Protein connections
        if not water_only:
            protein_coords = np.array([atm.coordinates for atm in protein])
            protein_indices = np.array([atm.index for atm in protein])
            protein_names = np.array([atm.name for atm in protein])

            tree = cKDTree(protein_coords)
            dist, indices = tree.query(water_coords, k=10, distance_upper_bound=dist_cutoff)

            for i, neighbors in enumerate(indices):
                for j, neighbor in enumerate(neighbors):
                    if dist[i, j] <= dist_cutoff:
                        site_status = (
                            'None' if self.active_site is None else
                            'active_site' if any(waters[i].resid == f.resid or protein[neighbor].resid == f.resid for f in self.active_site) else
                            'not_active_site'
                        )
                        if protein_names[neighbor] == 'O' or protein_names[neighbor] == 'N':
                            classification = 'backbone'
                        else:
                            classification = 'side-chain'
                        connections.append((protein_indices[neighbor], water_indices[i], protein_names[neighbor], 'WAT-PROT', site_status, classification))

        return connections


    def find_directed_connections(self, dist_cutoff=2.0, water_active=None, protein_active=None, active_site_only=False, water_only=False, angle_criteria=None):
        """
        Find directed connections using a K-D Tree.

        Parameters:
        - dist_cutoff: Distance cutoff (default 2.5)
        - water_active: Active site water molecule selection
        - protein_active: Active site protein molecule selection
        - active_site_only: Only find connections among active site atoms
        - water_only: Only find connections among waters
        - angle_criteria: Cutoff angle to define HBonds

        Returns:
        List of connections in the format:
        - connections[0] = index1
        - connections[1] = index2
        - connections[2] = atom name (of index1)
        - connections[3] = 'WAT-WAT' or 'WAT-PROT'
        - connections[4] = 'active_site' or 'not_active_site'
        """

        # Initialize empty list for connections
        connections = []

        # Select active site atoms if specified
        if active_site_only:
            waters = water_active
            protein = protein_active
        else:
            waters = self.water_molecules
            protein = self.protein_atoms

        # Gather water indices and coordinates
        water_H_indices = []
        water_H_coords = []
        water_H_names = []

        water_O_coords = []
        water_O_indices = []
        water_O_names = []

        for mol in waters:

            #Select status
            if self.active_site is None:
                site_status = 'None'
            elif mol in self.active_site:
                site_status = 'active_site'
            else:
                site_status = 'not_active_site'

            # Add H1 atom
            water_H_indices.append(mol.O.index)  #Use only O index
            water_H_coords.append(mol.H1.coordinates)
            water_H_names.append('H1')

            # Add H2 atom
            water_H_indices.append(mol.O.index) #Use only O index
            water_H_coords.append(mol.H2.coordinates)
            water_H_names.append('H2')

            # Add O
            water_O_indices.append(mol.O.index)
            water_O_coords.append(mol.O.coordinates)
            water_O_names.append('O')

        # Convert water_coords to a numpy array
        water_H_coords = np.array(water_H_coords).reshape(-1,3)
        water_O_coords = np.array(water_O_coords).reshape(-1,3)

        #Find protein-water connections
        if water_only == False:

            #Gather indices and coords
            protO_coords = []
            protO_indices = []        
            protO_names = []

            protH_coords = []
            protH_indices = []
            protH_names = []

            for atm in protein:
                if 'P' in atm.name or 'O' in atm.name or 'N' in atm.name or 'S' in atm.name:
                    protO_coords.append(atm.coordinates)
                    protO_indices.append(atm.index)
                    protO_names.append(atm.name)

                elif 'H' in atm.name:
                    protH_coords.append(atm.coordinates)
                    protH_indices.append(atm.index)
                    protH_names.append(atm.name)

            protH_coords = np.array(protH_coords).reshape(-1,3)
            protO_coords = np.array(protO_coords).reshape(-1,3)

            #Find distances between protein H and water O

            #Create KDTree using protein-H coordinates
            tree = cKDTree(protH_coords)

            #Query for distances with water O coordinates
            dist, indices = tree.query(water_O_coords, k=10, distance_upper_bound=dist_cutoff)

            for index_near, index_ref in enumerate(indices):
                for i, distance in enumerate(dist[index_near]):
                    #Check for cutoff, scipy will output infs if distance is too high
                    if distance <= dist_cutoff:
                        if not active_site_only:
                            # Determine active site status
                            if self.active_site is None:
                                site_status = 'None'
                            elif any(idx in [water_H_indices[index_near], protH_indices[index_ref[i]]] for idx in [f.index for f in self.active_site]):
                                site_status = 'active_site'
                            else:
                                site_status = 'not_active_site'
                        else: 
                            site_status = 'active_site'

                        #Append connections
                        if angle_criteria is None:
                            connections.append([protH_indices[index_ref[i]], water_O_indices[index_near], protH_names[index_ref[i]] , 'WAT-PROT', site_status])
                        else:
                            protein_hydrogen_coords = protH_coords[index_ref[i]]
                            water_oxygen_coords = water_O_coords[index_near]

                            protein_resid = [atm.resid for atm in self.protein_atoms if atm.index == protH_indices[index_ref[i]]][0]
                            protein_O_coordinates = [atm.coordinates for atm in self.protein_atoms if (atm.resid == protein_resid and 'H' not in atm.name)]
                            distances = [np.linalg.norm(protein_hydrogen_coords-f) for f in protein_O_coordinates]
                            arg = np.argmin(distances)

                            prot_heavy_coordinates = protein_O_coordinates[arg]

                            prot_water = protein_hydrogen_coords - water_oxygen_coords
                            prot_prot = prot_heavy_coordinates - protein_hydrogen_coords

                            cosine_angle = np.dot(prot_water, prot_prot) / (np.linalg.norm(prot_water) * np.linalg.norm(water1))
                            angle1 = np.degrees(np.arccos(cosine_angle))

                            if angle1 >= angle_criteria:
                                connections.append([protH_indices[index_ref[i]], water_O_indices[index_near], protH_names[index_ref[i]] , 'WAT-PROT', site_status])

            #Find distances between protein O,S,P,N and water H

            #Create KDTree using protein OSPN coordinates
            tree = cKDTree(protO_coords)

            #Query for distances with water-H coords
            dist, indices = tree.query(water_H_coords, k=10, distance_upper_bound=dist_cutoff)
            for index_near, index_ref in enumerate(indices):
                for i, distance in enumerate(dist[index_near]):
                    if distance <= dist_cutoff:        
                        if not active_site_only:
                            # Determine active site status
                            if self.active_site is None:
                                site_status = 'None'
                            elif any(idx in [water_H_indices[index_near], protO_indices[index_ref[i]]] for idx in [f.index for f in self.active_site]):
                                site_status = 'active_site'
                            else:
                                site_status = 'not_active_site'
                        else: 
                            site_status = 'active_site'

                        #Append connections
                        if angle_criteria is None:
                            connections.append([water_H_indices[index_near], protO_indices[index_ref[i]], water_H_names[index_near], 'WAT-PROT', site_status])
                        else:
                            protein_heavy_coords = protO_coords[index_ref[i]]
                            water_hydrogen_coords = water_H_coords[index_near]
                            water_o_coords = [water.O.coordinates for water in self.water_molecules if (water.O.index == water_H_indices[index_near])]

                            prot_water = protein_heavy_coords - water_o_coords
                            water1 = water_o_coords - water_hydrogen_coords

                            cosine_angle = np.dot(prot_water, water1) / (np.linalg.norm(prot_water) * np.linalg.norm(water1))
                            angle1 = np.degrees(np.arccos(cosine_angle))

                            if angle1 >= angle_criteria:
                                connections.append([water_H_indices[index_near], protO_indices[index_ref[i]], water_H_names[index_near], 'WAT-PROT', site_status])


            
        #Find distances between water O and water H

        #Create KDTree for water-O coords
        tree = cKDTree(water_O_coords)

        #Query for distances with water-H coords
        dist, indices = tree.query(water_H_coords, k=5, distance_upper_bound=dist_cutoff)

        
        for index_near, index_ref in enumerate(indices):
            for i, distance in enumerate(dist[index_near]):
                if distance <= dist_cutoff:
                    if not active_site_only:
                        # Determine active site status
                        if self.active_site is None:
                            site_status = 'None'
                        elif any(idx in [water_H_indices[index_near], water_O_indices[index_ref[i]]] for idx in [f.index for f in self.active_site]):
                            site_status = 'active_site'
                        else:
                            site_status = 'not_active_site'
                    else: 
                        site_status = 'active_site'

                    if water_H_indices[index_near] != water_O_indices[index_ref[i]]: #Check to make sure connection is not within the same water
                        
                        #Append connections
                        if angle_criteria is None:
                            if water_H_indices[index_near]<water_O_indices[index_ref[i]]:
                                print('IMPORTANT: CHECKING DUPLICATE CONNECTIONS')
                                connections.append([water_H_indices[index_near],water_O_indices[index_ref[i]], water_H_names[index_near], 'WAT-WAT', site_status])

                        else:
                            water_hydrogen_coords = water_H_coords[index_near]
                            water_o1_coords = water_O_coords[index_ref[i]]
                            
                            water_o2_coords = [water.O.coordinates for water in self.water_molecules if (water.O.index == water_H_indices[index_near])][0]
                            water1 = water_hydrogen_coords - water_o1_coords
                            water2 = water_hydrogen_coords - water_o2_coords

                            cosine_angle = np.dot(water1, water2) / (np.linalg.norm(water2) * np.linalg.norm(water1))
                            angle1 = np.degrees(np.arccos(cosine_angle))

                            if angle1 >= angle_criteria and water_H_indices[index_near]<water_O_indices[index_ref[i]]:
                                connections.append([water_H_indices[index_near],water_O_indices[index_ref[i]], water_H_names[index_near], 'WAT-WAT', site_status])

        return connections


    def generate_oxygen_network(self, box, msa_indexing=None, active_site_reference=None, active_site_radius=8.0, 
                                active_site_only=False, water_only=False, max_connection_distance=3.0):
        """
        Generate network based only on oxygens -- direct comparability to static structure networks

        Parameters:
        - active_site_reference: MDAnalysis atom selection language
        - water_only: creates a network with only waters

        Returns:
        networkx graph object
        """

        #Initialize graph
        G = nx.Graph()

        #Select active site
        if active_site_reference is not None:
            self.active_site, protein_active, water_active = self.select_active_site(active_site_reference, box=box, active_site_radius=active_site_radius)


        #Use MSA indexing
        if msa_indexing is not None:
            MSA_indices = msa_indexing
        else:
            MSA_indices = ['X'] * 10000 #Dummy list


        #Only include atoms in active site -- greatly increases performance
        if active_site_only:
            for molecule in water_active:
                G.add_node(molecule.O.index, pos=molecule.O.coordinates, atom_category='WAT', MSA=None) #have nodes on all oxygens

            if water_only == False:
                for molecule in protein_active:   
                    MSA_index = MSA_indices[molecule.resid-1]           
                    G.add_node(molecule.index, pos=molecule.coordinates, atom_category='PROTEIN', MSA=MSA_index)

            self.connections = self.find_connections(dist_cutoff=max_connection_distance, water_active=water_active, protein_active=protein_active, active_site_only=active_site_only, water_only=water_only)
            for connection in [f for f in self.connections if f[4]=='active_site']:
                G.add_edge(connection[0], connection[1], connection_type=connection[3], active_site=connection[4])

        #Include all atoms
        else:
            for molecule in self.water_molecules:
                G.add_node(molecule.O.index, pos=molecule.O.coordinates, atom_category='WAT', MSA=None) #have nodes on all oxygens

            if water_only == False:
                for molecule in self.protein_subset:
                    MSA_index = MSA_indices[molecule.resid-1]  
                    G.add_node(molecule.index, pos=molecule.coordinates, atom_category='PROTEIN', MSA=None)
            
            self.connections = self.find_connections(dist_cutoff=max_connection_distance, water_active=None, protein_active=None, active_site_only=False, water_only=water_only)

            for connection in self.connections:
                G.add_edge(connection[0], connection[1], connection_type=connection[3], active_site=connection[4])

        #Save as self.graph
        self.graph = G

        return self.graph

    def generate_directed_network(self, box, msa_indexing=None, active_site_reference=None, active_site_radius=8.0, 
                                  active_site_only=False, water_only=False, angle_criteria=None, max_connection_distance=3.0):
        """
        Generate directed graph using H -> O directionality

        Parameters:
        - active_site_reference: Any MDAnalysis atomselection language
        - active_site_only: Determines whether networks contain only active site atoms or all
        - angle_criteria: Angle cutoff (degrees)

        Returns:
        networkx graph object
        """
        G = nx.DiGraph() 
 
        #Select active site if a reference is given
        if active_site_reference is not None:
            self.active_site, protein_active, water_active = self.select_active_site(active_site_reference, box=box, active_site_radius=active_site_radius)

        #Use MSA indexing
        if msa_indexing is not None:
            MSA_indices = msa_indexing
        else:
            MSA_indices = ['X'] * 10000 #Dummy list

        #Only active site atoms in networks
        if active_site_only==True:
            #Add nodes
            for molecule in water_active:
                G.add_node(molecule.O.index, pos=molecule.O.coordinates, atom_category='WAT', MSA=None) #have nodes on all oxygens

            if water_only == False:
                for molecule in protein_active:              
                    MSA_index = MSA_indices[molecule.resid-1]
                    G.add_node(molecule.index, pos=molecule.coordinates, atom_category='PROTEIN', MSA=MSA_index)

            #Add edges
            self.connections = self.find_directed_connections(dist_cutoff=max_connection_distance, water_active=water_active, protein_active=protein_active, active_site_only=active_site_only, water_only=water_only, angle_criteria=angle_criteria)
            for connection in [f for f in self.connections if f[4]=='active_site']:
                G.add_edge(connection[0], connection[1], connection_type=connection[3], active_site=connection[4])

        #All atoms in network
        else:
            #Add nodes
            for molecule in self.water_molecules:
                G.add_node(molecule.O.index, pos=molecule.O.coordinates, atom_category='WAT', MSA=None) #have nodes on all oxygens

            if water_only == False:
                for molecule in self.protein_subset:
                    G.add_node(molecule.index, pos=molecule.coordinates, atom_category='PROTEIN', MSA=MSA_index)
            
            #Add edges
            self.connections = self.find_directed_connections(dist_cutoff=max_connection_distance, water_active=None, protein_active=None, active_site_only=False, water_only=water_only)
            for connection in self.connections:
                G.add_edge(connection[0], connection[1], connection_type=connection[3], active_site=connection[4])

        self.graph = G
        return G

    def get_density(self, selection='all'):
        """
        Requires self.graph to exist
        Calculates density as $N_{edges}/((N_{nodes}*N_{nodes}-1)/2)$ (ratio between edges and possible edges)

        Parameters:
        - selection: 'all', 'active_site', or 'not_active_site'

        Returns:
        float
        """       
        #Choose all subgraphs under particular criteria
        if selection=='all':
            S = self.graph
        else:
            S = self.graph.edge_subgraph([(edge1, edge2) for (edge1,edge2, data) in S.edges(data=True) if data['active_site']==selection])

        #Calculate density for subgraph
        nedges = S.number_of_edges()
        nnodes = S.number_of_nodes()
        density = nedges/((nnodes*(nnodes-1))/2) #density is ratio between edges and possible edges

        return density

    def get_connected_components(self, selection='all'):
        """
        Requires self.graph to exist
        Uses weakly_connected_components if graph is directed and connected_components if graph is undirected

        Parameters:
        - selection: 'all', 'active_site', or 'not_active_site'

        Returns:
        Numpy array of connected components
        """
        #Initiate empty array for connected components
        components = []

        #Choose all subgraphs under particular criteria
        if selection=='all':
            S = self.graph
        else:
            S = self.graph.edge_subgraph([(edge1, edge2) for (edge1,edge2, data) in self.graph.edges(data=True) if data['active_site']==selection])

        #Use weakly_connected_components for directed graph
        if self.graph.is_directed():
            for val in [len(cc) for cc in nx.weakly_connected_components(S)]:
                components.append(val)
        #Use connected_components for undirected
        else:
            for val in [len(cc) for cc in nx.connected_components(S)]:
                components.append(val)

        #Reshape components to make plotting easier
        components = np.array(components).reshape(-1,1)
        return components
    
    def get_interactions(self):
        interaction_dict = residue_analysis.get_interaction_counts(self)
        return(interaction_dict)
    
    def get_per_residue_interactions(self, selection='all'):
        residue_interaction_dict = residue_analysis.get_per_residue_interactions(self, selection)
        return(residue_interaction_dict)
    
    def get_CPL(self, selection='all', calculate_path='all', exclude_single_points=False):
        """
        Calculate characteristic path length (average shortest path length)

        Does not include connected components of length 1

        Parameters:
        - selection: 'all', 'active_site', or 'not_active_site'
        - calculate_path: 'all' or 'longest'
        - exclude_single_points: Indicate whether to exclude isolated points in connected components

        Returns:
        - CPL (float)
        """
        #Choose all subgraphs under particular criteria
        if selection=='all':
            S = self.graph
        else:
            S = self.graph.edge_subgraph([(edge1, edge2) for (edge1,edge2, data) in self.graph.edges(data=True) if data['active_site']==selection])

        try:
            CPL = nx.average_shortest_path_length(S)
        except nx.NetworkXError: #average_shortest_path_length will fail if graph is not connected
            if S.is_directed():

                #Change to undirected graph for nx.average_shortest_path_length
                S = S.to_undirected() 

            if exclude_single_points:
                cc = [f for f in nx.connected_components(S) if len(f)>1]
            else:
                cc = [f for f in nx.connected_components(S)]

            CPLs = []
            if calculate_path == 'all':
                for C in (S.subgraph(c).copy() for c in cc):
                    CPLs.append(nx.average_shortest_path_length(C))

                #Average over all calculated CPLs
                CPL = np.array(CPLs).mean()
            
            else:
                largest_cc = max(nx.connected_components(S), key=len)
                CPL = nx.average_shortest_path_length(largest_cc)

        return CPL
    
    def get_clustering_coefficient(self, selection='all'):

        #Choose all subgraphs under particular criteria
        if selection=='all':
            S = self.graph
        else:
            S = self.graph.edge_subgraph([(edge1, edge2) for (edge1,edge2, data) in self.graph.edges(data=True) if data['active_site']==selection])

        CC_dict = nx.clustering(S)
        return CC_dict

    def get_entropy(self, selection='all'):
        """
        Calculate graph entropy -- method taken from  https://stackoverflow.com/questions/70858169/networkx-entropy-of-subgraphs-generated-from-detected-communities

        Parameters:
        - selection: 'all', 'active_site', or 'not_active_site'


        Returns:
        Graph entropy (float)
        """
        def degree_distribuiton(G):
            vk = dict(G.degree())
            vk = list(vk.values())

            maxk = np.max(vk)
            mink = np.min(vk)

            kvalues = np.arange(0, maxk+1)

            Pk = np.zeros(maxk+1)
            for k in vk:
                Pk[k] = Pk[k] + 1

            Pk = Pk/sum(Pk)
            return kvalues, Pk
        
        if selection=='all':
            S = self.graph
        else:
            S = self.graph.edge_subgraph([(edge1, edge2) for (edge1,edge2, data) in self.graph.edges(data=True) if data['active_site']==selection])

        k, Pk = degree_distribuiton(S)

        H = 0
        for p in Pk:
            if p > 0:
                H = H - p*np.log2(p)

        return H
    
    def get_all_coordinates(self, selection='all', water_only=True):
        """
        Collect all coordinates from WaterNetwork object

        Parameters:
        - selection: 'all', 'active_site', or 'not_active_site'
        - water_only: Indicate whether to use water coordinates only
        """
        #Choose all subgraphs under particular criteria
        if selection=='all':
            #Find all coordinates -- only water oxygens
            coords = [np.array(f.O.coordinates) for f in self.water_molecules]

            if not water_only:
                coords.extend([np.array(f.coordinates) for f in self.protein_subset])

        else:
            #Find all coordinates -- only water oxygens
            coords = [np.array(f.O.coordinates) for f in self.active_site if type(f)==WaterMolecule]

            if not water_only:
                coords.extend([np.array(f.coordinates) for f in self.active_site if type(f)==OtherAtom])


        return coords


def get_clusters(coordinates, cluster='optics', min_samples=10, eps=0.0, n_jobs=1):
    from WatCon.find_conserved_networks import cluster_coordinates_only
    cluster_labels, cluster_centers = cluster_coordinates_only(coordinates, cluster, min_samples, eps, n_jobs)
    return cluster_labels, cluster_centers


def extract_objects_per_frame(pdb_file, trajectory_file, frame_idx, network_type, custom_selection, 
                              active_site_reference, active_site_radius, water_name, msa_indexing, 
                              active_site_only=False, directed=False, angle_criteria=None, max_connection_distance=3.0):
    """
    Function to return calculated network per frame
    
    All arguments are initially given to initialize_network

    Returns:
    WaterNetwork object
    """
    #Allow for custom residues in protein selection
    if custom_selection is None:
        custom_sel = ''
    else:
        custom_sel = f"or {custom_selection}" 

    #Allow for user-defined water name
    if water_name is None:
        water = 'resname HOH or resname WAT or resname SOL or resname H2O'
    else:
        water = f"resname {water_name}"

    #Create mda Universe
    u = mda.Universe(pdb_file, trajectory_file) 

    try:
        #Find maximum distance between edge of protein and middle of protein
        protein = u.select_atoms("protein")
        protein_com = protein.center_of_mass()
        distances_to_com = np.linalg.norm(protein.atoms.positions - protein_com, axis=1)
        max_distance = np.max(distances_to_com)

        #Separate key atom groups
        ag_wat = u.select_atoms(f'{water} and (sphzone {max_distance+0.5} protein)', updating=True)
        ag_protein = u.select_atoms(f'(protein {custom_sel}) and (name N* or name O* or name P* or name S*)', updating=True)
        ag_misc = u.select_atoms(f'not (protein or {water})', updating=True) #Keeping this for non-biological systems or where other solvent is important

    except:
        #Make water only
        print('No protein found, creating a network of only waters')
        ag_wat = u.select_atoms(f"resname HOH or resname WAT or resname SOL")

    #extract coordinates from frame of interest
    u.trajectory[frame_idx] 

    #Initiate active site reference atomgroup
    if active_site_reference is not None:
        active_site_residue = u.select_atoms(active_site_reference, updating=True)
    else:
        active_site_residue = None


    #Create network instance
    water_network = WaterNetwork()

    #Check which network type is desired
    if network_type == 'water-protein':
        water_only = False
        #Add protein atoms to network
        for atm in ag_protein.atoms:
            try:
                msa_resid = msa_indexing[atm.resid-1] #CHECK THIS 
            except:
                msa_resid = None
            water_network.add_atom(atm.index, atm.name, atm.resname, *atm.position, atm.resid, msa_resid)
    elif network_type == 'water-water':
        water_only = True
    else:
        raise ValueError("Provide a valid network type. Current valid network types include 'water-protein' or 'water-water'")

    #Add waters to network
    for mol in ag_wat.residues:
        ats = [atom for atom in mol.atoms]
        #Water molecules are objects which contain H1, H2, O atoms
        water_network.add_water(mol.resid, *ats, mol.resid)
    #Either find connections among only oxygens in waters or add hydrogens as well
    if directed:
        water_network.generate_directed_network(u.dimensions, msa_indexing, active_site_residue, active_site_radius=active_site_radius, 
                                                active_site_only=active_site_only, water_only=water_only, angle_criteria=angle_criteria, 
                                                max_connection_distance=max_connection_distance)
    else:
        water_network.generate_oxygen_network(u.dimensions, msa_indexing, active_site_residue, active_site_radius=active_site_radius, 
                                              active_site_only=active_site_only, water_only=water_only, max_connection_distance=max_connection_distance)
    
    return water_network


def initialize_network(topology_file, trajectory_file, structure_directory='.', network_type='water-protein', 
                       include_hydrogens=False, custom_selection=None, active_site_reference=None, active_site_only=False, 
                       active_site_radius=8.0, water_name=None, multi_model_pdb=False, max_distance=3.3, angle_criteria=None,
                       analysis_conditions='all', analysis_selection='all', project_networks=False, return_network=False, 
                       cluster_coordinates=False, clustering_method='hdbscan', min_cluster_samples=15, eps=None, msa_indexing=True, 
                       alignment_file='alignment.txt', combined_fasta='all_seqs.fa', fasta_directory='fasta', classify_water=False,
                       MSA_reference_pdb=None, water_reference_resids=None, num_workers=4):
    
    """
    Initialize network of choice. 

    Parameters:
    - topology_file: Name of topology file
    - trajectory_file: Name of trajectory file
    - structure_directory: Name of directory which contains structure files
    - network_type: 'water-water' or 'water-protein'
    - include_hydrogens: Indicate whether to make a directed graph with hydrogens
    - custom_selection: Any MDAnalysis selection language if you have custom residues (etc.) which you want included in your protein selection
    - active_site_reference: Any MDAnalysis selection language to define the center of your area of interest
    - active_site_only: Indicate whether to only include active site atoms
    - active_site_radius: Radius of active site centered on reference
    - water_name: Name of water residues, by default WatCon will recognize SOL, WAT, H2O, HOH
    - multi_model_pdb: Indicate whether pdb file has multiple models (most traditionally for NMR structures)
    - max_distance: Maximum distance between water atoms for an interaction to exist
    - angle_criteria: Specify extra angle criteria for calculating HBonds if hydrogens are present (recommend 150)
    - analysis_conditions: 'all' or dict specifying which analyses to perform
    - analysis_selection: 'all', 'active_site', or 'not_active_site' -- indicate what atoms to perform analysis on
    - project_networks: Indicate whether to generate pymol pml files for each network
    - return_network: Indicate whether to return the WaterNetwork objects (not recommended for large trajectories)
    - cluster_coordinates: Indicate whether to perform clustering on combined network
    - clustering_method: Clustering method -- 'hdbscan', 'dbscan', or 'optics'
    - min_cluster_samples: Minimum number of samples for cluster
    - eps: Eps distance for clustering
    - msa_indexing: Indicate whether to utilize/perform an MSA
    - alignment_file: Alignment file (PIR format)
    - combined_fasta: Combined fasta file
    - fasta_directory: Directory of fasta files
    - classify_water: Indicate whether to classify waters by angles and MSA
    - MSA_reference_pdb: PDB used as a reference to select residues
    - water_reference_resids: Resid IDs for water angle classification references
    - num_workers: Number of cores avaialble for parallelization

    Returns:
    Dictionary of calculated metrics per trajectory frame, cluster centers (if clustering is on)
    """
    def process_frame(frame_idx, coords=None, ref_coords=None, residues=None):
        """
        Internal function to make parallelizing each frame easier

        Returns:
        Calculated metrics for given frame
        """

        print(f"Processing frame {frame_idx}")


        #If an MSA has been performed
        if msa_indexing == True:

            #Assuming fasta file is named similarly to the pdb -- need sequence files for MSA alignment
            try:
                fasta_individual = [f for f in os.listdir(fasta_directory) if (topology_file.split('.')[0].split('_')[0] in f and 'fa' in f)][0]

                #Generate MSA if file does not exist and output MSA indices corresponding to partcicular sequence
                msa_indices = sequence_processing.generate_msa_alignment(alignment_file, combined_fasta, os.path.join(fasta_directory, fasta_individual))

            #If MSA cannot be done, use residues as msa_indices
            except:
                print(f'Warning: Could not find an equivalent fasta file for {pdb_file}. Check your naming schemes!')
                msa_indices = residues
            
        else:
            msa_indices = None

        #Create WaterNetwork object
        network = extract_objects_per_frame(pdb_file, traj_file, frame_idx, network_type, custom_selection, active_site_reference, 
                                            active_site_radius=active_site_radius, water_name=water_name, msa_indexing=msa_indices, 
                                            active_site_only=active_site_only, directed=include_hydrogens, angle_criteria=angle_criteria, 
                                            max_connection_distance=max_distance)

        metrics = {}
        #Calculate metrics as per user input
        if analysis_conditions['density'] == True:
            metrics['density'] = network.get_density(selection=analysis_selection)
        if analysis_conditions['connected_components'] == True:
            metrics['connected_components'] = network.get_connected_components(selection=analysis_selection)
        if analysis_conditions['interaction_counts'] == True:
            metrics['interaction_counts'] = network.get_interactions(selection=analysis_selection)
        if analysis_conditions['per_residue_interactions'] == True:
            metrics['per_residue_interaction'] = network.get_per_residue_interactions(selection=analysis_selection)
        if analysis_conditions['characteristic_path_length'] == True:
            metrics['characteristic_path_length'] = network.get_CPL(selection=analysis_selection)
        if analysis_conditions['graph_entropy'] == True:
            metrics['entropy'] = network.get_entropy(selection=analysis_selection)
        if analysis_conditions['clustering_coefficient'] == True:
            metrics['clustering_coefficient'] = network.get_clustering_coefficient(selection=analysis_selection)
        #clustering coefficient -- https://www.annualreviews.org/content/journals/10.1146/annurev-physchem-050317-020915

        #Classify waters
        if classify_water: 
            if msa_indices is None:
                print('No MSA indices found, waters cannot be classified without a common indexing reference!')
                raise ValueError
            
            #Select reference coords
            if len(ref_coords) == 1:
                ref2_coords = None
            else:
                ref2_coords = ref_coords[1]

            classification_dict = residue_analysis.classify_waters(network, ref1_coords=ref_coords[0], ref2_coords=ref2_coords)

            #Write classification dict into a csv file
            with open(f'CLASSIFICATION_DYNAMIC.csv', 'a') as FILE:
                for key, val in classification_dict.items():
                        FILE.write(f"{frame_idx},{key},{val[0]},{val[1]}\n")

        #Save coodinates for clustering
        if coords is not None:
            if active_site_only == True:
                selection = 'active_site'
            else:
                selection='all'

            coords = network.get_all_coordinates(selection=selection)
            metrics['coordinates'] = np.array(coords).reshape(-1,3)

        #Create pymol projections for each frame
        if project_networks:
            import WatCon.visualize_structures as visualize_structures
            visualize_structures.pymol_project_oxygen_network(network, filename=f'{frame_idx+2}.pml', out_path='pymol_projections', active_site_only=active_site_only)

        #Do not do this for large trajectories
        if return_network:
            return (network, metrics)
        else:
            return(metrics)
        

    
    #Get pdb and traj file
    pdb_file = os.path.join(structure_directory, topology_file)
    traj_file = os.path.join(structure_directory, trajectory_file)

    if analysis_conditions == 'all':
        analysis_conditions = {
            'density': True,
            'connected_components': True,
            'interaction_counts': True,
            'per_residue_interactions': True,
            'characteristic_path_length': True,
            'graph_entropy': True,
            'clustering_coefficient': True
        }

    #Create universe object just once to get number of frames
    try:
        u = mda.Universe(pdb_file, traj_file)
        frames = len(u.trajectory)
        residues = u.residues.resids.tolist()

    except:
        if multi_model_pdb == True:
            u = mda.Universe(pdb_file, multiframe=True)
            frames = len(u.trajectory)
        else:
            print('Warning: You are attempting to create networks for only one structure. Consider using generate_static_networks instead')
            u = mda.Universe(pdb_file)
            frames = 0

    #Initialize empty list to collect coordinates if clustering
    if cluster_coordinates:
        coords = []
    else:
        coords=None

    #Initialize ref_coords if classifying water
    ref_coords = [None]
    if classify_water:
        #Find ref_coords if particular residue is indicated
        if water_reference_resids is not None:
            u = mda.Universe(os.path.join(structure_directory, MSA_reference_pdb))

            #Allows for maximum 2 reference resids
            if isinstance(water_reference_resids, list):
                ref_coords = [u.select_atoms(f"resid {water_reference_resids[0]} and name CA").positions, u.select_atoms(f"resid {water_reference_resids[1]} and name CA").positions]
            else:
                ref_coords = [u.select_atoms(f"resid {water_reference_resids} and name CA").positions]

        #Write header for classification file
        with open(f'CLASSIFICATION_DYNAMIC.csv', 'w') as FILE:
            FILE.write('Frame Index,Resid,MSA_Resid,Index_1,Index_2,Protein_Atom,Classification,Angle_1,Angle_2\n')


    #Parallelized so there is one worker allocated for each frame
    network_metrics = Parallel(n_jobs=num_workers)(delayed(process_frame)(frame_idx, coords, ref_coords, residues) for frame_idx in range(frames))

    #Cluster coordinates after networks are created returns metrics and centers
    if cluster_coordinates:
        print('Clustering...')

        # Assuming network_metrics contains dictionaries with 'coordinates' arrays
        coordinates = [
            f['coordinates'] for f in network_metrics if f['coordinates'].shape[1] == 3
        ]
        combined_coordinates = np.concatenate([arr for arr in coordinates], axis=0)

        cluster_labels, cluster_centers = get_clusters(combined_coordinates, cluster=clustering_method, min_samples=min_cluster_samples, eps=eps, n_jobs=num_workers)
        project_clusters(cluster_centers, filename_base='MD', separate_files=False)
        return (network_metrics, cluster_centers)

    #Return only metrics if no clustering
    return(network_metrics)
