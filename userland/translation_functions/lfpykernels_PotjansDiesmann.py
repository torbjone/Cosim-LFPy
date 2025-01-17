#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Class to find LFP kernels from the Potians-Diesmann network model,
so that LFPs can be calculated directly from spikes received through
the EBRAINS multiscale Co-simulation framework.

Copyright (C) 2023 Computational Neuroscience Group, NMBU.

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

"""
import os
import json
import numpy as np
import matplotlib
matplotlib.use("AGG")
import matplotlib.pyplot as plt
import scipy.stats as st

from mpi4py import MPI
comm = MPI.COMM_WORLD
rank = comm.Get_rank()
size = comm.Get_size()

# (NEURON MUST BE IMPORTED AFTER MPI SOMETIMES)
import neuron
from lfpykernels import KernelApprox, GaussCylinderPotential

import userland
from userland.parameters.Potjans.stimulus_params import stim_dict
from userland.parameters.Potjans.network_params import net_dict
from userland.parameters.Potjans.sim_params import sim_dict

# TODO refactor paths for saving the simulation results and plots

use_case_folder = userland.__path__[0]
mod_folder = os.path.join(use_case_folder, 'mod')
mech_loaded = neuron.load_mechanisms(mod_folder)

if rank==0 and not mech_loaded:
    os.system(f'cd {mod_folder} && nrnivmodl && cd -')
    mech_loaded = neuron.load_mechanisms(mod_folder)
    os.system(f'cd -')

comm.Barrier()
assert mech_loaded

binzegger_file = os.path.join(use_case_folder, 'parameters',
                              'binzegger_connectivity_table.json')
morphology_folder = os.path.join(use_case_folder, 'models',
                                 'morphologies', 'stretched')
template_folder = os.path.join(use_case_folder, 'models', 'morphologies')


class PotjansDiesmannKernels:
    """
    Class to find LFP kernels from the Potians-Diesmann network model,
    so that LFPs can be calculated directly from spikes received through
    the EBRAINS multiscale Co-simulation framework.

    Note that this class is use-case specific, and depends on the parameters
    specified by the dictionaries under 'userland.parameters.Potjans'

    For more in-depth information about the kernel method, see
        * Hagen et al. (2022) https://doi.org/10.1371/journal.pcbi.1010353

    Convention:
        X: Presynaptic population (e.g. L4E)
        x: Presynaptic subpopulation (e.g. p4)
        Y: Postsynaptic population
        y: Postsynaptic subpopulation
    """

    def __init__(self, spike_recorder_ids, sim_savefolder=None, fig_folder=None,
                 overwrite_kernels=False):

        # The parameters of the model and simulation are given by these
        # dictionaries:
        self.sim_dict = sim_dict
        self.net_dict = net_dict
        self.stim_dict = stim_dict

        self.neuron_params = net_dict['neuron_params']
        self.dt = sim_dict['sim_resolution']
        self.tvec = np.arange(int(sim_dict['t_sim'] / self.dt + 1)) * self.dt

        if sim_savefolder is None:
            self.sim_saveforlder = os.path.join(use_case_folder, 'models', 'sim_results')
            os.makedirs(self.sim_saveforlder, exist_ok=True)
        else:
            self.sim_saveforlder = sim_savefolder

        if fig_folder is None:
            self.fig_folder = os.path.join(use_case_folder, 'models', 'figures')
            os.makedirs(self.fig_folder, exist_ok=True)
        else:
            self.fig_folder = fig_folder
        self.overwrite_kernels = overwrite_kernels
        self.plot_conn_data = True
        self.plot_kernels = True
        self.plot_firing_rate = False

        with open(binzegger_file) as f:
            conn_dict = json.load(f)
        self.conn_data = conn_dict['data']

        self._extract_neuron_parameters()
        self._prepare_populations(spike_recorder_ids)
        self._find_layer_specific_pathways()
        self._set_extracellular_elec_params()
        self._set_kernel_params()

        self._calculate_all_pathway_kernels()
        # The calculation of pathway kernels must be completed on all
        # ranks before we continue:
        comm.Barrier()
        self._load_pathway_kernels()
        self._find_kernels()

        self.fr_dict = {pop_name: np.zeros(len(self.tvec))
                        for pop_name in self.presyn_pops}
        self.lfp = np.zeros((self.num_elecs, len(self.tvec)))
        # self._load_firing_rates_from_file()
        # self.plot_lfps()

    def _extract_neuron_parameters(self):
        """
        Extracts useful single-neuron parameters from the parameter
        dictionaries
        """
        # Extract some useful neuron parameters:
        self.tau_syn = self.neuron_params['tau_syn']
        self.tau_m = self.neuron_params['tau_m']
        self.C_m = self.neuron_params['C_m']
        self.E_L = self.neuron_params['E_L']

        # Convert postsynaptic potential into postsynaptic current
        # function "postsynaptic_potential_to_current", Potjans2014/helpers.py
        sub = 1. / (self.tau_syn - self.tau_m)
        pre = self.tau_m * self.tau_syn / self.C_m * sub
        frac = (self.tau_m / self.tau_syn) ** sub
        self.PSC_over_PSP = 1. / (pre * (frac ** self.tau_m -
                                         frac ** self.tau_syn)) * 1e-3  # nA

    def _set_kernel_params(self):
        """
        Sets the paramters of the kernel method. These can be regarded
        as fairly static, and should not need to be changed for different
        network configurations
        """
        # Ignore first X ms of simulation in kernel prediction:
        self.TRANSIENT = 200
        self.t_X = self.TRANSIENT
        self.tau = 50  # time lag relative to spike for kernel predictions
        self.kernel_length = 2 * int(self.tau / self.dt)
        self.g_eff = False

    def _set_extracellular_elec_params(self):
        """
        Sets the parameters of the extracellular electrode, indicating
        where the LFP is calculated
        """
        self.num_elecs = 16
        # class RecExtElectrode parameters:
        self.elec_params = dict(
            x=np.zeros(self.num_elecs),  # x-coordinates of contacts
            y=np.zeros(self.num_elecs),  # y-coordinates of contacts
            z=np.arange(self.num_elecs) * (-100),  # z-coordinates of contacts
            sigma=0.3,  # extracellular conductivity (S/m)
            method="linesource"  # use line sources
        )

        self.dz = np.abs(self.elec_params['z'][1] -
                         self.elec_params['z'][0])

    def _prepare_populations(self, spike_recorder_ids):
        """
        Set up the modelled neural populations, and declare
        their respective morphologies etc.
        For more information on the chosen morphologies and layer boundaries,
        see Hagen et al. (2016) https://doi.org/10.1093/cercor/bhw237
        """
        pop_names = self.net_dict['populations']

        self.presyn_pops = pop_names + ['TC']
        self.postsyn_pops = pop_names

        self.pop_IDs = {spike_recorder_id: pop_name
                        for spike_recorder_id, pop_name in zip(spike_recorder_ids, self.presyn_pops)}

        self.pop_clrs = {pop_name: plt.cm.rainbow(i / (len(self.presyn_pops) - 1))
                         for i, pop_name in enumerate(self.presyn_pops)}

        if self.net_dict['N_scaling'] != 1 and rank == 0:
            print("Scaling population sizes by factor {:1.2f}".format(
                self.net_dict['N_scaling']))

        pop_sizes = np.array(self.net_dict['full_num_neurons']) * \
            self.net_dict['N_scaling']
        self.pop_sizes = np.r_[pop_sizes,
                               [self.stim_dict["num_th_neurons"]]]

        self.layers = ["1", "23", "4", "5", "6"]
        self.layer_boundaries = {
            "1": [0.0, -81.6],
            "23": [-81.6, -587.1],
            "4": [-587.1, -922.2],
            "5": [-922.2, -1170.0],
            "6": [-1170.0, -1491.7]
            }

        self.layer_mids = [np.mean(self.layer_boundaries[layer])
                           for layer in self.layers]
        self.layer_thicknesses = [self.layer_boundaries[layer][0] -
                                  self.layer_boundaries[layer][1]
                                  for layer in self.layers]

        self.subpop_dict = {
            'L23E': ['p23'],
            'L23I': ['b23', 'nb23'],
            'L4E': ['p4', 'ss4(L23)', 'ss4(L4)'],
            'L4I': ['b4', 'nb4'],
            'L5E': ['p5(L23)', 'p5(L56)'],
            'L5I': ['b5', 'nb5'],
            'L6E': ['p6(L4)', 'p6(L56)'],
            'L6I': ['b6', 'nb6'],
            'TC': ['TCs', 'TCn'],
        }

        self.subpop_mapping_dict = {
            'p23': 'L23E',
            'b23': 'L23I',
            'nb23': 'L23I',
            'p4': 'L4E',
            'ss4(L23)': 'L4E',
            'ss4(L4)': 'L4E',
            'b4': 'L4I',
            'nb4': 'L4I',
            'p5(L23)': 'L5E',
            'p5(L56)': 'L5E',
            'b5': 'L5I',
            'nb5': 'L5I',
            'p6(L4)': 'L6E',
            'p6(L56)': 'L6E',
            'b6': 'L6I',
            'nb6': 'L6I',
            'TCs': 'TC',
            'TCn': 'TC',
        }
        self.subpop_names = self.subpop_mapping_dict.keys()

        self.morph_map = {
            # 'p23': 'L23E_oi24rpy1.hoc',
            # 'b23': 'L23I_oi38lbc1.hoc',
            # 'nb23': 'L23I_oi38lbc1.hoc',
            'L23E': 'L23E_oi24rpy1.hoc',  #
            'L23I': 'L23I_oi38lbc1.hoc',

            # 'p4': 'L4E_53rpy1.hoc',
            # 'ss4(L23)': 'L4E_j7_L4stellate.hoc',
            # 'ss4(L4)': 'L4E_j7_L4stellate.hoc',
            # 'b4': 'L4I_oi26rbc1.hoc',
            # 'nb4': 'L4I_oi26rbc1.hoc',
            'L4E': 'L4E_53rpy1.hoc',
            'L4I': 'L4I_oi26rbc1.hoc',

            # 'p5(L23)': 'L5E_oi15rpy4.hoc',
            # 'p5(L56)': 'L5E_j4a.hoc',
            # 'b5': 'L5I_oi15rbc1.hoc',
            # 'nb5': 'L5I_oi15rbc1.hoc',
            'L5E': 'L5E_j4a.swc',
            'L5I': 'L5I_oi15rbc1.hoc',

            # 'p6(L4)': 'L6E_51-2a.CNG.hoc',
            # 'p6(L56)': 'L6E_oi15rpy4.hoc',
            # 'b6': 'L6I_oi15rbc1.hoc',
            # 'nb6': 'L6I_oi15rbc1.hoc',
            'L6E': 'L6E_oi15rpy4.hoc',
            'L6I': 'L6I_oi15rbc1.hoc'
        }

        self.conn_probs = np.zeros((len(self.postsyn_pops),
                                    len(self.presyn_pops)))
        self.conn_probs[:len(self.postsyn_pops),
                        :len(self.postsyn_pops)] = self.net_dict['conn_probs']
        self.conn_probs[:, -1] = self.stim_dict['conn_probs_th']

    def _find_layer_specific_pathways(self):
        """
        We need to find the normalized layer-specific input for each
        connection pathway (e.g. L4E -> L5E).
        Each population has subpopulations making up given
        fractions of the population.
        Many sup-populations given in the connection
        dictionary ('binzegger_connectivity_table.json') are not used.
        For each layer of each post-synaptic subpopulation,
        the data is normalized, but this includes
        input from many unused subpopulations.
        We need to take into account the relative abundance of
        each post-synaptic subpopulation.
        """

        # Prepare layer-specific input distribution array for each
        # post-synaptic subpopulation
        syn_pathways_subpops = {}
        for postsyn_pop in self.postsyn_pops:
            for postsyn_subpop in self.subpop_dict[postsyn_pop]:
                for presyn_pop in self.presyn_pops:
                    subpop_pathway_name = f'{postsyn_subpop}:{presyn_pop}'
                    syn_pathways_subpops[subpop_pathway_name] = np.zeros(5)

        # Find number of inputs to each layer for each
        # post-synaptic subpopulation:
        for postsyn_pop in self.postsyn_pops:
            for postsyn_subpop in self.subpop_dict[postsyn_pop]:
                for l_idx, layer in enumerate(self.layers):
                    if layer in self.conn_data[postsyn_subpop]['syn_dict']:
                        conn_data_yL = self.conn_data[postsyn_subpop]['syn_dict'][layer]
                        sum_ = 0
                        # Total number of synapses to this layer of this subpopulation:
                        k_yL = conn_data_yL['number of synapses per neuron']
                        for p in conn_data_yL:
                            if not p == 'number of synapses per neuron':
                                sum_ += conn_data_yL[p]
                            # Include all considered presynaptic populations:
                            if p in self.subpop_names:
                                p_yxL = conn_data_yL[p]
                                # Number of synapses from presynaptic subpopulation
                                # to this layer of the postsynaptic subpopulation:
                                k_xyL = (p_yxL / 100) * k_yL

                                # Number of inputs from each included presynaptic
                                # population to this layer is summed:
                                presyn_pop = self.subpop_mapping_dict[p]
                                subpop_pathway_name = f'{postsyn_subpop}:{presyn_pop}'
                                syn_pathways_subpops[subpop_pathway_name][l_idx] += k_xyL
                        # Sanity check that sum of all percentage-wise input to this layer of this
                        # subpopulation sums to 100 %:
                        assert np.round(sum_) == 100.0

        # Normalize layer-specific input for each postsynaptic population:
        for subpop_pathway_name in syn_pathways_subpops.keys():
            if np.sum(syn_pathways_subpops[subpop_pathway_name]) > 0.0:
                syn_pathways_subpops[subpop_pathway_name] /= np.sum(
                    syn_pathways_subpops[subpop_pathway_name])

        # Make a dictionary with the relative fraction of
        # each subpopulation within a population:
        subpop_rel_frac = {}
        for pop_name in self.presyn_pops:
            rel_frac = []
            for subpop in self.subpop_dict[pop_name]:
                rel_frac.append(self.conn_data[subpop]['occurrence'])
            subpop_rel_frac[pop_name] = np.array(rel_frac) / np.sum(rel_frac)

        # Add postsynaptic subpopulations weighted by relative occurrence.
        # The resulting dictionary 'syn_pathways' contains the
        # normalized layer-specific synaptic distribution
        # for each synaptic pathway:
        self.syn_pathways = {}
        for postsyn_pop in self.postsyn_pops:
            for presyn_pop in self.presyn_pops:
                pathway_name = f'{postsyn_pop}:{presyn_pop}'
                self.syn_pathways[pathway_name] = np.zeros(5)
                for p_idx_, postsyn_subpop in enumerate(
                        self.subpop_dict[postsyn_pop]):
                    subpop_pathway_name = f'{postsyn_subpop}:{presyn_pop}'
                    rel_frac_ = subpop_rel_frac[postsyn_pop][p_idx_]
                    self.syn_pathways[pathway_name] += syn_pathways_subpops[
                                                                subpop_pathway_name] * rel_frac_

        if self.plot_conn_data and rank == 0:
            # Plot layer-specific connectivity data, similar
            # to Fig. 5D in Hagen et al. (2016) https://doi.org/10.1093/cercor/bhw237
            fig = plt.figure(figsize=[10, 10])
            fig.subplots_adjust(wspace=0.3, hspace=0.2, bottom=0.05,
                                top=0.95, right=0.98, left=0.05)
            plot_idx = 1
            num_rows = 4
            num_cols = 4
            for postsyn_pop in self.postsyn_pops:
                for postsyn_subpop in self.subpop_dict[postsyn_pop]:
                    conn_matrix = np.zeros((len(self.layers),
                                            len(self.presyn_pops)))
                    presyn_pops_reordered = ["TC"] + self.postsyn_pops
                    for pre_idx, presyn_pop in enumerate(presyn_pops_reordered):
                        subpop_pathway_name = f'{postsyn_subpop}:{presyn_pop}'
                        conn_matrix[:, pre_idx] = syn_pathways_subpops[
                            subpop_pathway_name]

                    ax = fig.add_subplot(num_rows, num_cols, plot_idx,
                                         title=f'$y$={postsyn_subpop}')
                    ax.set_xticks(np.arange(len(presyn_pops_reordered)))
                    ax.set_xticklabels(presyn_pops_reordered, rotation=-90)
                    ax.set_yticks(np.arange(len(self.layers)))
                    ax.set_yticklabels(self.layers)
                    ax.set_xlabel("$X$")
                    ax.set_ylabel("$L$")
                    ax.imshow(conn_matrix, cmap="hot", vmax=1, vmin=0)

                    plot_idx += 1
            plt.savefig(os.path.join(self.fig_folder,
                                     "layer_specific_conn_data.png"))

    def _calculate_one_pathway_kernel(self, postsyn_pop, presyn_pop):
        """
        Calculate the LFP kernel for one specific connection pathway from the
        presynaptic population to the postsynaptic population
        """

        postsyn_pop_idx = np.where([p_ == postsyn_pop
                                    for p_ in self.postsyn_pops])[0][0]
        presyn_pop_idx = np.where([p_ == presyn_pop
                                   for p_ in self.presyn_pops])[0][0]

        postsyn_l_idx = np.where([l_ == postsyn_pop[1:-1]
                                  for l_ in self.layers])[0][0]
        pathway_name = f'{postsyn_pop}:{presyn_pop}'
        filename = os.path.join(self.sim_saveforlder,
                                f'kernel_{pathway_name}.npy')

        if np.abs(self.conn_probs[postsyn_pop_idx, presyn_pop_idx]) < 1e-9:
            # No pathway from presyn_pop to postsyn_pop
            return

        if os.path.isfile(filename) and not self.overwrite_kernels:
            # Kernel already exists, and we do not overwrite it
            return

        layered_input = self.syn_pathways[pathway_name]

        if np.sum(layered_input) < 1e-9:
            # If this has happened the connection probability is non-zero,
            # but no connection data was extracted from data_dict.
            # This happens in one case, with a low connection probability.
            # Unclear why this happens, but we just assume the connection is
            # in the layer of the postsynaptic cell.
            # print(f"{presyn_pop} to {postsyn_pop}: {layered_input} " +
            #      "while connection probability is non-zero: " +
            #      f"{self.conn_probs[postsyn_pop_idx, presyn_pop_idx]}")
            layered_input[postsyn_l_idx] = 1.0

        # Parameters for a chosen representative post-synaptic cell model:
        cell_params = dict(
            morphology=os.path.join(morphology_folder,
                                    self.morph_map[postsyn_pop]),
            templatename='LFPyCellTemplate',
            templatefile=os.path.join(template_folder, 'LFPyCellTemplate.hoc'),
            v_init=self.E_L,
            cm=1.0,
            Ra=150,
            passive=True,
            passive_parameters=dict(g_pas=1. / (self.tau_m * 1E3),  # assume cm=1
                                    e_pas=self.E_L),
            nsegs_method='lambda_f',
            lambda_f=100,
            dt=self.dt,
            delete_sections=True,
            templateargs=None,
        )

        # Parameters for the postsynaptic population.
        # For mathematical convenience, the spatial spread of the
        # somas and synapses in the depth direction (z-axis) are treated
        # as gaussians, with a standard deviation
        # of layer_thickness/spatial_spread_dz. The spatial_spread_dz parameter
        # is important in deciding the resulting LFP amplitude, with a larger
        # value giving higher LFP amplitudes.
        self.spatial_spread_dz = 4
        population_area = 1000**2  # Potians Diesmann model has area of 1000 µm^2
        population_params = dict(
            radius=np.sqrt(population_area / np.pi),  # population radius
            loc=self.layer_mids[postsyn_l_idx],  # population center along z-axis
            scale=self.layer_thicknesses[postsyn_l_idx] / self.spatial_spread_dz)  # SD along z-axis

        # See the documentation of LFPykernels for a better description of
        # these paramters:
        rotation_args = {'x': 0.0, 'y': 0.0}
        sections = "allsec" if "I" in presyn_pop else ["dend", "apic"]
        syn_pos_params = [dict(section=sections,
                               fun=[st.norm] * len(self.layers),
                               funargs=[dict(loc=self.layer_mids[l_idx],
                                             scale=self.layer_thicknesses[l_idx] / self.spatial_spread_dz)
                                        for l_idx in range(len(self.layers))],
                               funweights=layered_input
                               )]

        gauss_cyl_potential = GaussCylinderPotential(
            cell=None,
            z=self.elec_params['z'],
            sigma=self.elec_params['sigma'],
            R=population_params['radius'],
            sigma_z=population_params['scale'],
        )

        if presyn_pop == 'TC':
            PSP_mean = self.stim_dict['PSP_th']
            delay_mean = self.stim_dict['delay_th_mean']
            delay_rel_std = self.stim_dict['delay_th_rel_std']
        else:
            PSP_mean = self.net_dict['PSP_matrix_mean'][postsyn_pop_idx, presyn_pop_idx]
            delay_mean = self.net_dict['delay_matrix_mean'][postsyn_pop_idx, presyn_pop_idx]
            delay_rel_std = self.net_dict['delay_rel_std']

        C_YX = self.conn_probs[postsyn_pop_idx].copy()
        if self.net_dict['K_scaling'] != 1:
            # see Potjans2014/helpers.py,
            # function: adjust_weights_and_input_to_synapse_scaling
            # if rank == 0:
            #     print('Synapses are adjusted to compensate scaling of indegrees')
            PSP_mean /= np.sqrt(self.net_dict['K_scaling'])
            C_YX *= self.net_dict['K_scaling']

        weight = PSP_mean * self.PSC_over_PSP
        delay_params = [{'a': (self.dt - delay_mean) / delay_rel_std,
                         'b': np.inf,
                         'loc': delay_mean,
                         'scale': delay_rel_std}]

        syn_params = [dict(weight=weight, syntype='ExpSynI', tau=self.tau_syn)]

        # Create KernelApprox object, see LFPykernels for documentation
        kernel = KernelApprox(
            X=[presyn_pop],
            Y=postsyn_pop,
            N_X=np.array([self.pop_sizes[presyn_pop_idx]]),
            N_Y=self.pop_sizes[postsyn_pop_idx],
            C_YX=C_YX,
            cellParameters=cell_params,
            rotationParameters=rotation_args,
            populationParameters=population_params,
            multapseFunction=st.norm,
            multapseParameters=[dict(loc=1, scale=0.001)],  # Ignores multapses
            delayFunction=st.truncnorm,
            delayParameters=delay_params,
            synapseParameters=syn_params,
            synapsePositionArguments=syn_pos_params,
            extSynapseParameters=None,
            nu_ext=None,
            n_ext=None,
            nu_X=None,
            conductance_based=False,
        )

        # Make kernel predictions and update container dictionary
        H_XY = kernel.get_kernel(probes=[gauss_cyl_potential],
                                 Vrest=self.E_L, dt=self.dt, X=presyn_pop,
                                 t_X=self.t_X, tau=self.tau,
                                 g_eff=self.g_eff, fir=False)
        k_ = H_XY['GaussCylinderPotential']

        # Save kernel to file for later use
        np.save(filename, k_)

        if self.plot_kernels:
            t_k = np.arange(k_.shape[1]) * self.dt

            cell = kernel.cell

            plt.close("all")
            fig = plt.figure(figsize=[16, 5])
            fig.subplots_adjust(left=0.05, right=0.98, top=0.82, wspace=0.4)
            fig.suptitle(f"LFP kernel for input to {postsyn_pop} from {presyn_pop}")
            ax_m = fig.add_subplot(151, aspect=1, xlim=[-500, 500],
                                   ylim=[-1600, 200],
                                   title="postsynaptic neuron")
            ax_s = fig.add_subplot(152, ylim=[-1600, 200],
                                   title="synaptic input density\nBinzegger data")
            ax_g = fig.add_subplot(153, ylim=[-1600, 200],
                                   title="inferred gaussian input profile")
            ax_w = fig.add_subplot(154, ylim=[-1600, 200],
                                   title="per. comp synaptic weight")
            ax_k = fig.add_subplot(155, ylim=[-1600, 200],
                                   title="LFP kernel")

            [ax_m.axhline(boundary[0], c='gray', ls='--')
             for boundary in self.layer_boundaries.values()]
            ax_m.axhline(self.layer_boundaries["6"][1], c='gray', ls='--')

            ax_m.plot(cell.x.T, cell.z.T, c='k')

            poss_idx = cell.get_idx(section="allsec", z_min=-1e9, z_max=1e9)
            p = np.zeros_like(cell.area)
            p[poss_idx] = cell.area[poss_idx]
            mod = np.zeros(poss_idx.shape)

            xs_ = [0]
            ys_ = [0]
            for l_idx, layer in enumerate(self.layers):
                df = st.norm(loc=self.layer_mids[l_idx],
                             scale=self.layer_thicknesses[l_idx] / 2)
                # Normalize to have same area, regardless of layer thickhness
                mod += df.pdf(x=cell.z[poss_idx].mean(axis=-1)
                              ) * layered_input[l_idx]

                xs_.extend([layered_input[l_idx] /
                            self.layer_thicknesses[l_idx]] * 2)
                ys_.extend(self.layer_boundaries[layer])

            xs_.append(0)
            ys_.append(self.layer_boundaries["6"][1])
            ax_s.plot(xs_, ys_, c=self.pop_clrs[presyn_pop], label=presyn_pop)

            ax_g.plot(mod, cell.z.mean(axis=1), '.',
                      c=self.pop_clrs[presyn_pop])

            ax_w.plot(kernel.comp_weight, cell.z.mean(axis=1), 'k.')

            k_norm = np.max(np.abs(k_))

            for elec_idx in range(self.num_elecs):
                ax_k.plot(t_k, k_[elec_idx] / k_norm * self.dz +
                          self.elec_params["z"][elec_idx],
                          c='k')

            ax_k.plot([30, 30], [-1000, -1000 + self.dz], c='gray', lw=1.5)
            ax_k.text(31, -1000 + self.dz / 2, f"{k_norm * 1000: 1.2f} µV",
                      color="gray")

            fig.legend(frameon=False, ncol=6, loc=(0.3, 0.75))
            plt.savefig(os.path.join(self.fig_folder,
                                     f"fig_pathways_syn_input_"
                                     f"{postsyn_pop}_{presyn_pop}.png"))

    def _calculate_all_pathway_kernels(self):
        """
        Calculate all pathway kernels using MPI. If a kernel already
        exist on disc, they are not recalculated unless
        self.overwrite_kernels is True.
        """
        task_idx = 0
        # Loop through all synaptic pathways in the model and calculate kernels:
        for postsyn_pop_idx, postsyn_pop in enumerate(self.postsyn_pops):
            for presyn_pop_idx, presyn_pop in enumerate(self.presyn_pops):
                if task_idx % size == rank:
                    print(f"{presyn_pop} to {postsyn_pop} on rank {rank}")
                    self._calculate_one_pathway_kernel(postsyn_pop, presyn_pop)
                task_idx += 1

    def _load_pathway_kernels(self):
        """
        Loads the LFP kernels from each connection pathway from file
        """
        self.H = {}
        for postsyn_pop_idx, postsyn_pop in enumerate(self.postsyn_pops):
            for presyn_pop_idx, presyn_pop in enumerate(self.presyn_pops):
                pathway_name = f'{postsyn_pop}:{presyn_pop}'
                f_name = os.path.join(self.sim_saveforlder,
                                      f'kernel_{pathway_name}.npy')
                if os.path.isfile(f_name):
                    self.H[pathway_name] = np.load(f_name)

    def _find_kernels(self):
        """
        Summing the LFP kernels of each presynaptic populations, so that the
        LFP can be found by convolving the presynaptic firing rates with
        the corresponding summed LFP kernel.
        """

        self.pop_kernels = {}
        for pop_idx, pop_name in enumerate(self.presyn_pops):
            self.pop_kernels[pop_name] = np.zeros((self.num_elecs,
                                                   self.kernel_length))
            for pathway_name in self.H.keys():
                if pathway_name.endswith(pop_name):
                    if self.H[pathway_name] is not None:
                        self.pop_kernels[pop_name] += self.H[pathway_name]

    def _load_firing_rates_from_file(self):
        """
        Load saved firing rates from file.
        NOTE: This is only ment for internal testing, and not for use with
        the Co-simulation framework, where spikes are received in realtime
        and not loaded from simulation after the simulation has finished.
        """
        self.firing_rate_path = os.path.join(use_case_folder, 'models',
                                             'Potjans', 'data')
        self.pop_gids = {}

        sim_start = self.sim_dict['t_presim']
        sim_end = sim_start + self.sim_dict['t_sim'] + self.dt

        self.bins = np.arange(sim_start, sim_end, self.dt)

        with open(os.path.join(self.firing_rate_path,
                               'population_nodeids.dat')) as f:
            self.gid_data = np.array([l_.split() for l_ in f.readlines()],
                                     dtype=int)
            for pop_idx, pop_name in enumerate(self.presyn_pops):
                # TC population is not in gid_data if not modelled
                if pop_idx < self.gid_data.shape[0]:
                    self.pop_gids[pop_name] = self.gid_data[pop_idx]

        pop_spike_times, self.firing_rates = self._load_and_return_spikes()

        if self.plot_firing_rate:
            plt.close('all')
            fig = plt.figure(figsize=[10, 10])
            ax1 = fig.add_subplot(111, xlim=[675, 750])
            fr_norm = 40

            for pop_idx, pop_name in enumerate(self.presyn_pops):
                ax1.plot(self.bins[:-1],
                         self.firing_rates[pop_name] / fr_norm + pop_idx,
                         c=self.pop_clrs[pop_name], label=pop_name)
            fig.legend(ncol=8, frameon=False)
            plt.savefig(os.path.join(self.fig_folder, "pop_firing_rates.png"))

    def _return_pop_name_from_gid(self, gid):
        for pop_name in self.presyn_pops:
            if self.pop_gids[pop_name][0] <= gid <= self.pop_gids[pop_name][1]:
                return pop_name

    def _load_and_return_spikes(self):
        """
        Load saved spikes from file.
        NOTE: This is only ment for internal testing, and not for use with
        the Co-simulation framework, where spikes are received in realtime
        and not loaded from simulation after the simulation has finished.
        """
        firing_rates = {}
        pop_spike_times = {pop_name: [] for pop_name in self.presyn_pops}
        fr_files = [f for f in os.listdir(self.firing_rate_path)
                    if f.startswith('spike_recorder-')]
        for f_ in fr_files:
            with open(os.path.join(self.firing_rate_path, f_)) as f:
                d_ = [d__.split('\t') for d__ in f.readlines()[3:]]

                gids, times = np.array(d_, dtype=float).T
                for pop_idx, pop_name in enumerate(self.presyn_pops):
                    if pop_idx < self.gid_data.shape[0]:
                        p_spikes_mask = (self.pop_gids[pop_name][0] <= gids) & \
                                        (gids <= self.pop_gids[pop_name][1])
                        pop_spike_times[pop_name].extend(times[p_spikes_mask])

        for pop_idx, pop_name in enumerate(self.presyn_pops):
            pop_spike_times[pop_name] = np.sort(pop_spike_times[pop_name])
            fr__, _ = np.histogram(pop_spike_times[pop_name], bins=self.bins)
            firing_rates[pop_name] = fr__

        return pop_spike_times, firing_rates

    def plot_final_results(self, fig_name='summary_LFP'):
        """
        Plot final results after simulation end, with both the firing rate
        of each individual population and the resulting LFP.
        """
        plt.close('all')
        fig = plt.figure(figsize=[8, 8])
        fig.subplots_adjust(right=0.85, hspace=0.5)
        xlim = [np.max([0, self.tvec[-1] - 400]), self.tvec[-1]]
        ax_fr = fig.add_subplot(211, title="firing rates", xlabel="time (ms)",
                                 xlim=xlim)

        max_fr = np.max([np.max(np.abs(fr_)) for fr_ in self.fr_dict.values()])

        for p_idx, pop in enumerate(self.presyn_pops):
            ax_fr.plot(self.tvec, self.fr_dict[pop] / max_fr + p_idx, label=pop)
        ax_fr.legend(frameon=False, loc=(1.0, 0.45))

        ax_lfp = fig.add_subplot(212, title="LFP", xlabel="time (ms)",
                                 ylabel="depth (µm)",
                                 ylim=[-1600, 200], xlim=xlim)

        lfp_norm = np.max(np.abs(self.lfp))
        for elec_idx in range(self.num_elecs):
            ax_lfp.plot(self.tvec, self.lfp[elec_idx] / lfp_norm * self.dz +
                        self.elec_params["z"][elec_idx], c='k')

        ax_lfp.plot([xlim[1], xlim[1]], [-100, -100 + self.dz], c='gray',
                    lw=1.5, clip_on=False)
        ax_lfp.text(xlim[1], -100 + self.dz / 2, f"{lfp_norm * 1000: 1.2f} µV",
                    color="gray", ha='left', va='center')
        simplify_axes(fig.axes)
        fig.savefig(os.path.join(self.fig_folder, f"{fig_name}.png"))

    # def update_lfp_DEPRECATED(self, lfp, t_idx, firing_rate):
    #     """
    #     Calculate LFP resulting from LFP kernel, given a
    #     firing rate at a given time index.
    #
    #     This method is DEPRECATED, and only exist for reference.
    #     """
    #     # For every new timestep the predicted LFP signal is made
    #     # one timestep longer
    #
    #     # self.lfp.append([0] * self.num_elecs)
    #     if lfp is None:
    #         lfp = np.zeros((self.num_elecs, self.kernel_length - 1))
    #
    #     lfp = np.append(lfp, np.zeros((self.num_elecs, 1)), axis=1)
    #     # Find the time indexes where the LFP is calculated:
    #     window_idx0 = t_idx - int(self.kernel_length / 2)
    #     window_idx1 = t_idx + int(self.kernel_length / 2)
    #     sig_idx0 = 0 if window_idx0 < 0 else window_idx0
    #     sig_idx1 = window_idx1
    #     k_idx0 = -window_idx0 if window_idx0 < 0 else 0
    #
    #     # This is essentially a manual convolution, one timestep at the time,
    #     # between the firingrate and the kernel
    #     for p_idx, pop in enumerate(self.presyn_pops):
    #         for elec_idx in range(self.num_elecs):
    #             lfp_ = firing_rate[pop] * self.pop_kernels[pop][elec_idx][k_idx0:]
    #             lfp[elec_idx, sig_idx0:sig_idx1] += lfp_
    #     return lfp

    # def get_firingrate_from_buffer(self, buffer, fr_dict):
    #     """ Get firing rate from buffer.  """
    #
    #     for pop_ID in set(buffer[:, 0]):
    #         if int(pop_ID) not in fr_dict.keys():
    #             fr_dict[int(pop_ID)] = np.zeros(len(self.tvec))
    #         times = buffer[buffer[:, 0] == pop_ID][:, 2]
    #         # assert times[-1] <= self.tvec[-1], "spiketime after simulation end"
    #
    #         # Ignore spiketimes that comes after the last time step.
    #         # times = times[times <= self.tvec[-1]]
    #
    #         for t_ in times:
    #             if t_ > self.tvec[-1]:
    #                 break
    #             spiketime_idx = np.argmin(np.abs(t_ - self.tvec))
    #             fr_dict[int(pop_ID)][spiketime_idx] += 1
    #     return fr_dict

    def save_final_results(self):
        """
        Save firing rate and LFP to file after simulation end.
        """
        np.save(os.path.join(self.sim_saveforlder, 'firing_rate.npy'),
                             self.fr_dict)
        np.save(os.path.join(self.sim_saveforlder, 'lfp.npy'),
                             self.lfp)

    def update(self, buffer):
        """
        Gets buffer spike data from the co-simulation framework,
        and calculates the resulting firing rate and LFP.

        Parameters
        ---------
        buffer: ndarray of
            buffer with spike data, with shape (num_spike_events, 3),
            where the first column is the ID of spike recorder (used to
            identify population name), the second column is the
            neuron_ID (not used), and the third column is the spike time.
        """
        if len(buffer) == 0:
            return

        # Find smallest and largest time in buffer, so we can
        # update the corresponding part of the LFP signal
        t0, t1 = np.min(buffer[:, 2]), np.max(buffer[:, 2])
        t0_idx = np.argmin(np.abs(t0 - self.tvec))
        t1_idx = np.argmin(np.abs(t1 - self.tvec)) + 1

        for pop_ID in set(buffer[:, 0]):
            pop_name = self.pop_IDs[pop_ID]
            spiketimes = buffer[buffer[:, 0] == pop_ID][:, 2]

            for t_ in spiketimes:
                if t_ > self.tvec[-1]:
                    # Ignore spiketimes that comes after the last time step.
                    break
                spiketime_idx = np.argmin(np.abs(t_ - self.tvec))
                self.fr_dict[pop_name][spiketime_idx] += 1

            fr_ = self.fr_dict[pop_name][t0_idx:t1_idx]
            window_idx0 = t0_idx #- int(self.kernel_length / 2)
            window_idx1 = t1_idx + int(self.kernel_length / 2) - 1
            sig_idx0 = 0 if window_idx0 < 0 else window_idx0
            sig_idx1 = window_idx1 if window_idx1 < len(self.tvec) else len(self.tvec)

            for elec_idx in range(self.num_elecs):
                k_ = self.pop_kernels[pop_name][elec_idx, :]
                lfp_ = np.convolve(k_, fr_, mode='full')[int(self.kernel_length / 2):]
                if len(self.lfp[elec_idx, sig_idx0:sig_idx1]) == len(lfp_):
                    self.lfp[elec_idx, sig_idx0:sig_idx1] += lfp_
                else:
                    self.lfp[elec_idx, sig_idx0:sig_idx1] += lfp_[:(sig_idx1 - sig_idx0)]

    def sanity_test_convolution(self):
        lfp_postcalc = np.zeros((self.num_elecs, len(self.tvec)))
        for pop_idx, pop_name in enumerate(self.presyn_pops):
            fr_ = self.fr_dict[pop_name]
            for elec_idx in range(self.num_elecs):
                k_ = self.pop_kernels[pop_name][elec_idx, :]
                lfp_ = np.convolve(k_, fr_, mode='full')[int(self.kernel_length / 2):]
                lfp_postcalc[elec_idx, :] += lfp_[:len(lfp_postcalc[elec_idx, :])]
        print(np.max(np.abs(self.lfp - lfp_postcalc)))

        plt.close('all')
        fig = plt.figure(figsize=[8, 8])
        fig.subplots_adjust(right=0.85, hspace=0.5)
        xlim = [0, 1000]
        ax_fr = fig.add_subplot(211, title="firing rates", xlabel="time (ms)",
                                 xlim=xlim)

        max_fr = np.max([np.max(np.abs(fr_)) for fr_ in self.fr_dict.values()])

        for p_idx, pop in enumerate(self.presyn_pops):
            ax_fr.plot(self.tvec, self.fr_dict[pop] / max_fr + p_idx, label=pop)
        ax_fr.legend(frameon=False, loc=(1.0, 0.45))

        ax_lfp = fig.add_subplot(212, title="LFP", xlabel="time (ms)",
                                 ylabel="depth (µm)",
                                 ylim=[-1600, 200], xlim=xlim)

        lfp_norm = np.max(np.abs(self.lfp))
        for elec_idx in range(self.num_elecs):
            ax_lfp.plot(self.tvec, self.lfp[elec_idx] / lfp_norm * self.dz +
                        self.elec_params["z"][elec_idx], c='k')
            ax_lfp.plot(self.tvec, lfp_postcalc[elec_idx] / lfp_norm * self.dz +
                        self.elec_params["z"][elec_idx], c='r')

        ax_lfp.plot([xlim[1], xlim[1]], [-100, -100 + self.dz], c='gray',
                    lw=1.5, clip_on=False)
        ax_lfp.text(xlim[1], -100 + self.dz / 2, f"{lfp_norm * 1000: 1.2f} µV",
                    color="gray", ha='left', va='center')
        simplify_axes(fig.axes)
        fig.savefig(os.path.join(self.fig_folder, f"sanity_test.png"))


def simplify_axes(axes):
    """
    Plotting helper function to make nicer plots. It hides top and right axes
    of axes.
    """

    if not type(axes) is list:
        axes = [axes]

    for ax in axes:
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        ax.get_xaxis().tick_bottom()
        ax.get_yaxis().tick_left()


if __name__ == '__main__':

    # This is for debugging purposes. Original data is overwritten by
    # dummy version.
    sim_dict['t_sim'] = 1000
    dummy_buffer = np.zeros((3, 3))
    dummy_buffer[:, 0] = [7719., 7719., 7722.]
    dummy_buffer[:, 2] = [10, 20, 30]

    spike_recorder_ids = np.arange(7718, 7726)
    PD_kernels = PotjansDiesmannKernels(spike_recorder_ids)
    PD_kernels.pop_kernels[PD_kernels.pop_IDs[7719.]] *= 0
    PD_kernels.pop_kernels[PD_kernels.pop_IDs[7719.]][:, int(PD_kernels.kernel_length/2) + 1] = 1e-5

    PD_kernels.pop_kernels[PD_kernels.pop_IDs[7722.]] *= 0
    PD_kernels.pop_kernels[PD_kernels.pop_IDs[7722.]][:, int(PD_kernels.kernel_length/2) + 1] = 1e-5

    PD_kernels.update(dummy_buffer)
    PD_kernels.plot_final_results('dummy_control')
    PD_kernels.sanity_test_convolution()
