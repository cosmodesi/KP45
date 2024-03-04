##################################################################################################################
# 1. Calculate the propagator defined as P_cross/(bias*P_init), given the galaxy mock and IC.
# 2. We firstly generate meshes for the IC and tracer, then we use MeshFFTCorrelator to calculate the correlator. 
# *** Note that for the MultiGrid recon, it is better to set nmesh = a*2^n, e.g. 3*2^9, to avoid any weird performance. ***
# 3. The propagator information can be obtained from the correlator. You can check show_propagator.ipynb as an example.
# (To run the code, you can check the script propagator_nzweight_perlmutter.sl.)
#
import os, sys
import fitsio, asdf
import numpy as np
from scipy import interpolate
from pathlib import Path
from cosmoprimo.fiducial import DESI
from pyrecon import utils
from pyrecon.metrics import MeshFFTCorrelator, MeshFFTPropagator, CatalogMesh
from pypower.mesh import ArrayMesh
from pypower import setup_logging, mpi, MeshFFTPower
import argparse


def fkp_weights(nz, P0=10000):
    return 1 / (1 + nz * P0)

def interpolate_nz(z, nz, zmin, zmax):
    zbins = np.linspace(zmin, zmax, 41)
    nz_list = []
    for z0, z1 in zip(zbins[0:-1], zbins[1:]):
        zmask = (z>z0)&(z<z1)
        nz_mean = np.mean(nz[zmask])
        nz_list.append(nz_mean)
    
    zmid = (zbins[0:-1]+zbins[1:])/2.0  # middle point for each z bin
    zmid[0] = zbins[0]    # change the first and last point to cover the whole redshift range
    zmid[-1] = zbins[-1]
    nz_array = np.array(nz_list)
    res = interpolate.InterpolatedUnivariateSpline(zmid, nz_array)
    return res
        
    

def main():
    parser = argparse.ArgumentParser(description="Calculate propagator of the LRG cutsky mock")

    parser.add_argument('--cap', required=True, type=str, help='sgc, ngc or both, the footprint of DESI.')
    parser.add_argument('--nmesh', required=True, type=int, help='number of mesh size in the propagator calculation.')
    parser.add_argument('--input_ic_dir', required=True, type=str, help='input IC directory')
    parser.add_argument('--input_tracer_dir', required=True, type=str, help='input tracer directory')
    parser.add_argument('--output_dir', required=True, type=str, help='output data directory')
    parser.add_argument('--zmin', required=True, type=float, help='the minimum redshift.')
    parser.add_argument('--zmax', required=True, type=float, help='the maximum redshift.')
    parser.add_argument('--recon_nmesh', required=True, type=int, help='the nmesh in recon.')
    parser.add_argument("--add_nzweight", required=True, type=str, help='Add the nz weight for IC or not. True or False.')
    
    args=parser.parse_args()
    
    os.environ.setdefault('NUMEXPR_MAX_THREADS', os.environ.get('OMP_NUM_THREADS', '1'))
    os.environ.setdefault('NUMEXPR_NUM_THREADS', os.environ.get('OMP_NUM_THREADS', '1'))
    
    setup_logging()
    mpicomm = mpi.COMM_WORLD
    mpiroot = 0
    
    cap = args.cap
    nmesh = args.nmesh
    ic_dir = args.input_ic_dir
    data_dir = args.input_tracer_dir
    output_dir = args.output_dir
    zmin, zmax = args.zmin, args.zmax
    recon_nmesh = args.recon_nmesh
    add_nzweight = args.add_nzweight
    
    cosmo = DESI()
    node, phase = '000', '000'
    #recon_algos = ['MultiGrid']#, 'IterativeFFT']
    #recon_algos = ['IterativeFFT','IterativeFFTParticle']
    recon_algos = ['MultiGrid', 'IterativeFFT', 'IterativeFFTParticle']
    conventions = ['recsym', 'reciso']
    
    smooth_radii = ['7.5', '10', '15']
    #smooth_radii = ['10', '15']
    
    los = 'firstpoint'
    bias = 1.99    
    
    zcubic = 0.8      # the snapshot of a cubic box used for the cutsky mock
    # rescale IC density to low z
    zeff = (zmin + zmax)/2.0
    growth_rate = cosmo.hubble_function(zcubic)/cosmo.hubble_function(zeff) * (1+zeff)/(1+zcubic) * cosmo.growth_rate(zcubic)
    
    ells = (0, 2, 4)
    kedges = np.arange(0.01, 1.0, 0.005)
    muedges = np.linspace(0., 1., 21)


    # calculate Pk from different reconstruction schemes
    for smooth_radius in smooth_radii:
        for recon_algo in recon_algos:
            if 'IterativeFFT' in recon_algo and phase == '000':
                niterations = [3, 5, 7]
            else:
                niterations = [3]
                
            for niter in niterations:
            
                for convention in conventions:
                    if 'IterativeFFT' in recon_algo:                     
                        fname_appendix = f"{cap.upper()}_{zmin}z{zmax}_shift_{recon_algo}_randoms20X_nmesh{recon_nmesh}_smooth{smooth_radius}_pad1.5_niter{niter}_{convention}_f{growth_rate:.3f}_b{bias:.2f}"
                           
                    else:
                        fname_appendix = f"{cap.upper()}_{zmin}z{zmax}_shift_{recon_algo}_randoms20X_nmesh{recon_nmesh}_smooth{smooth_radius}_pad1.5_{convention}_f{growth_rate:.3f}_b{bias:.2f}"

                    if mpicomm.rank == mpiroot:
                        positions = {}
                        positions_rec = {}
                        # ----- read reconstructed data         
                        data_fn = Path(data_dir, f"cutsky_LRG_z0.800_AbacusSummit_base_c000_ph000_{fname_appendix}.fits")
                        data = fitsio.read(data_fn)
                        print(f'Data size: {data.size}')


                        # ----- read reconstructed randoms
                        rand_fn = Path(data_dir, f"cutsky_LRG_random20X_ph000_{fname_appendix}.fits")
                        randoms = fitsio.read(rand_fn)
                        print(f'Randoms size: {randoms.size}')

                        # ----- calculate pre-recon positions of data 
                        dis = cosmo.comoving_radial_distance(data['Z'])
                        pos = utils.sky_to_cartesian(dis, data['RA'], data['DEC'])
                        positions['data'] = pos
                        # ----- calculate pre-recon positions of randoms
                        dis = cosmo.comoving_radial_distance(randoms['Z'])
                        pos = utils.sky_to_cartesian(dis, randoms['RA'], randoms['DEC'])
                        positions['randoms'] = pos

                        # ----- calculate post-recon positions of data 
                        dis = cosmo.comoving_radial_distance(data['Z_REC'])
                        pos = utils.sky_to_cartesian(dis, data['RA_REC'], data['DEC_REC'])
                        positions_rec['data'] = pos
                        # ----- calculate post-recon positions of randoms
                        dis = cosmo.comoving_radial_distance(randoms['Z_REC'])
                        pos = utils.sky_to_cartesian(dis, randoms['RA_REC'], randoms['DEC_REC'])
                        positions_rec['randoms'] = pos     

                        w_fkp_data = fkp_weights(data['NZ_MAIN'])
                        w_fkp_randoms = fkp_weights(randoms['NZ_MAIN'])

                        ##spl_nz_data = interpolate.InterpolatedUnivariateSpline(data['Z'], data['NZ_MAIN'])
                        spl_nz_data = interpolate_nz(data['Z'], data['NZ_MAIN'], zmin, zmax)

                        # Read initial condition density field
                        ##data_fn = Path(ic_dir, f'cutsky_0.01_z_1.65_ic_AbacusSummit_base_c000_ph000_{cap.upper()}.fits') 
                        data_fn = Path(ic_dir, f'ic_dens_N576_AbacusSummit_base_c000_ph000_Y5_{cap.upper()}_z{zmin:.2f}_{zmax:.2f}.fits') 
                        ic_data = fitsio.read(data_fn)

                        Z_ic  = ic_data['Z_COSMO']
                        zmask = (Z_ic>zmin)&(Z_ic<zmax)

                        distance = cosmo.comoving_radial_distance(Z_ic[zmask])
                        positions_ic = utils.sky_to_cartesian(distance, ic_data['RA'][zmask], ic_data['DEC'][zmask])

                        #factor = cosmo.growth_factor(zeff) / cosmo.growth_factor(99.0)
                        data_fn = Path(f"/global/cfs/cdirs/desi/public/cosmosim/AbacusSummit/ic/AbacusSummit_base_c000_ph{phase}", 'ic_dens_N576.asdf')
                        with asdf.open(data_fn, lazy_load=False) as af:
                            growth_table = af['header']['GrowthTable']

                        factor = growth_table[zcubic] / growth_table[99.0]
                        print("IC rescale factor:", factor)

                        print("IC rescale factor:", factor)
                        #rescaled_mesh_ic = mesh_ic * factor
                        #rescaled_mesh_ic += 1.0
                        weights_ic = 1.0 + (ic_data['ONEplusDELTA'][zmask]-1) * factor       # Need to rescale DENSITY 
                        if add_nzweight == 'True':
                            """ scaled by the data's n(z); assume the pre-/post-recon n(z) is close to each other """
                            dndz = spl_nz_data(Z_ic[zmask])
                            weights_ic *= dndz

                            weights_ic_random = dndz
                        else:
                            weights_ic_random = None

                        del(ic_data)

                    else:
                        positions = {'data': None, 'randoms': None}
                        positions_rec = {'data': None, 'randoms': None}
                        w_fkp_data = None
                        w_fkp_randoms = None  

                        positions_ic = None
                        weights_ic = None
                        weights_ic_random = None


                    rescaled_mesh_ic = CatalogMesh(positions_ic, data_weights=weights_ic, randoms_positions=positions_ic, randoms_weights=weights_ic_random, boxpad=1.5, nmesh=nmesh, resampler='tsc', interlacing=2, position_type='pos', mpicomm=mpicomm, mpiroot=mpiroot)  ## we should consider the weights on the uniform IC positions 

                    # compute correlator/propagator for pre-recon
                    #pk_ic = MeshFFTPower(rescaled_mesh_ic, edges=kedges, ells=ells, los=los).poles
                    #result = cal_multipoles(pk_ic, remove_shotnoise=False)  # for an uniform IC, no need to consider "shotnoise"!!
                    power_ic = MeshFFTPower(rescaled_mesh_ic, edges=kedges, ells=ells, los=los, shotnoise=0.)

                    Path(output_dir).mkdir(parents=True, exist_ok=True)

                    fn = power_ic.mpicomm.bcast(os.path.join(output_dir, f'pk_cutsky_IC_{cap.upper()}_c000_ph000_{zmin}z{zmax}_nmesh{nmesh}.npy'), root=0)
                    fn_txt = power_ic.mpicomm.bcast(os.path.join(output_dir, f'pk_cutsky_IC_{cap.upper()}_c000_ph000_{zmin}z{zmax}_nmesh{nmesh}.txt'), root=0)

                    power_ic.save(fn)
                    power_ic.poles.save_txt(fn_txt)
                    power_ic.mpicomm.Barrier()

                    # paint reconstructed positions to mesh
                    # we need to consider the argument of shifted_positions (except for the "rsd" recon convention)
                    mesh_data_recon = CatalogMesh(positions_rec['data'], data_weights=w_fkp_data, randoms_positions=positions['randoms'], randoms_weights=w_fkp_randoms, shifted_positions=positions_rec['randoms'], shifted_weights=w_fkp_randoms, boxsize=rescaled_mesh_ic.boxsize, boxcenter=rescaled_mesh_ic.boxcenter, nmesh=nmesh, resampler='tsc', interlacing=2, position_type='pos', mpicomm=mpicomm, mpiroot=mpiroot)

                    # compute correlator/propagator
                    correlator_post = MeshFFTCorrelator(mesh_data_recon, rescaled_mesh_ic, edges=(kedges, muedges), los=los)

                    fn = correlator_post.num.mpicomm.bcast(os.path.join(output_dir, f"correlator_LRG_c000_ph000_{fname_appendix}_nmesh{nmesh}.npy"), root=0)
                    fn_txt = correlator_post.num.mpicomm.bcast(os.path.join(output_dir, f"correlator_LRG_c000_ph000_{fname_appendix}_nmesh{nmesh}.txt"), root=0)
                    correlator_post.save(fn)
                    correlator_post.save_txt(fn_txt)
                    correlator_post.mpicomm.Barrier()
                
    # for pre-recon result
    # paint pre-reconstructed positions to mesh
    mesh_data_pre = CatalogMesh(positions['data'], data_weights=w_fkp_data, randoms_positions=positions['randoms'], randoms_weights=w_fkp_randoms, boxsize=rescaled_mesh_ic.boxsize, boxcenter=rescaled_mesh_ic.boxcenter, nmesh=nmesh, resampler='tsc', interlacing=2, position_type='pos', mpicomm=mpicomm, mpiroot=mpiroot)

    correlator_pre = MeshFFTCorrelator(mesh_data_pre, rescaled_mesh_ic, edges=(kedges, muedges), los=los)

    fn = correlator_pre.num.mpicomm.bcast(os.path.join(output_dir, f"correlator_LRG_c000_ph000_{cap.upper()}_{zmin}z{zmax}_Pre_recon_randoms20X_f{growth_rate:.3f}_b{bias:.2f}_nmesh{nmesh}.npy"), root=0)
    fn_txt = correlator_pre.num.mpicomm.bcast(os.path.join(output_dir, f"correlator_LRG_c000_ph000_{cap.upper()}_{zmin}z{zmax}_Pre_recon_randoms20X_f{growth_rate:.3f}_b{bias:.2f}_nmesh{nmesh}.txt"), root=0)
    
    correlator_pre.save(fn)
    correlator_pre.save_txt(fn_txt)
    correlator_pre.mpicomm.Barrier()
                
if __name__ == '__main__':
    main()

