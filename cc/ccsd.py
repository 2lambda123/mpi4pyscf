#!/usr/bin/env python

import time
import ctypes
import numpy
from pyscf import gto
from pyscf import lib
from pyscf import ao2mo
from pyscf.ao2mo import _ao2mo
from pyscf.cc import ccsd
from pyscf import __config__

from mpi4pyscf.lib import logger
from mpi4pyscf.lib import diis
from mpi4pyscf.tools import mpi

BLKMIN = getattr(__config__, 'cc_ccsd_blkmin', 4)
MEMORYMIN = getattr(__config__, 'cc_ccsd_memorymin', 2000)

comm = mpi.comm
rank = mpi.rank


@mpi.parallel_call
def kernel(mycc, eris=None, t1=None, t2=None, max_cycle=50, tol=1e-8,
           tolnormt=1e-6, verbose=None):
    log = logger.new_logger(mycc, verbose)
    cput1 = cput0 = (time.clock(), time.time())
    _sync_(mycc)

    eris = getattr(mycc, '_eris', None)
    if eris is None:
        mycc.ao2mo(mycc.mo_coeff)
        eris = mycc._eris
    if t1 is None and t2 is None:
        t1, t2 = mycc.get_init_guess(eris)
    elif t2 is None:
        t2 = mycc.get_init_guess(eris)[1]

    eold = 0
    vec_old = 0
    eccsd = 0
    if mycc.diis:
        adiis = diis.DistributedDIIS(mycc)
        adiis.space = mycc.diis_space
    else:
        adiis = None

    conv = False
    for istep in range(max_cycle):
        t1new, t2new = mycc.update_amps(t1, t2, eris)
        normt = _diff_norm(mycc, t1new, t2new, t1, t2)
        t1, t2 = t1new, t2new
        t1new = t2new = None

        t1, t2 = mycc.run_diis(t1, t2, istep, normt, eccsd-eold, adiis)
        eold, eccsd = eccsd, mycc.energy(t1, t2, eris)
        log.info('cycle = %d  E(CCSD) = %.15g  dE = %.9g  norm(t1,t2) = %.6g',
                 istep+1, eccsd, eccsd - eold, normt)
        cput1 = log.timer('CCSD iter', *cput1)
        if abs(eccsd-eold) < tol and normt < tolnormt:
            conv = True
            break
    log.timer('CCSD', *cput0)
    return conv, eccsd, t1, t2


def update_amps(mycc, t1, t2, eris):
    time0 = time.clock(), time.time()
    log = logger.Logger(mycc.stdout, mycc.verbose)
    fock = eris.fock
    cpu1 = time0

    t1T = t1.T
    t2T = numpy.asarray(t2.transpose(2,3,0,1), order='C')
    nvir_seg, nvir, nocc = t2T.shape[:3]
    t1 = t2 = None
    ntasks = mpi.pool.size
    vlocs = [_task_location(nvir, task_id) for task_id in range(ntasks)]
    vloc0, vloc1 = vlocs[rank]
    assert(vloc1-vloc0 == nvir_seg)

    def _rotate_vir_block(buf):
        for task_id, buf in _rotate_tensor_block(buf):
            loc0, loc1 = vlocs[task_id]
            yield task_id, buf, loc0, loc1

    fswap = lib.H5TmpFile()
    wVooV = numpy.zeros((nvir_seg,nocc,nocc,nvir))
    eris_voov = numpy.asarray(eris.ovvo).transpose(1,0,3,2)
    tau  = t2T * .5
    tau += numpy.einsum('ai,bj->abij', t1T[vloc0:vloc1], t1T)
    for task_id, tau, p0, p1 in _rotate_vir_block(tau):
        cpu1 = log.timer('aaa0   %d %s' % (task_id, lib.current_memory()), *cpu1)
        wVooV += lib.einsum('bkic,cajk->bija', eris_voov[:,:,:,p0:p1], tau)
        cpu1 = log.timer('aaa1   %d %s' % (task_id, lib.current_memory()), *cpu1)
    fswap['wVooV1'] = wVooV
    wVooV = tau = None

    wVOov = eris_voov
    eris_VOov = eris_voov - eris_voov.transpose(0,2,1,3)*.5
    tau  = t2T.transpose(2,0,3,1) - t2T.transpose(3,0,2,1)*.5
    tau -= numpy.einsum('ai,bj->jaib', t1T[vloc0:vloc1], t1T)
    for task_id, tau, p0, p1 in _rotate_vir_block(tau):
        cpu1 = log.timer('bbb0   %d %s' % (task_id, lib.current_memory()), *cpu1)
        wVOov += lib.einsum('dlkc,kcjb->dljb', eris_VOov[:,:,:,p0:p1], tau)
        cpu1 = log.timer('bbb1   %d %s' % (task_id, lib.current_memory()), *cpu1)
    fswap['wVOov1'] = wVOov
    wVOov = tau = eris_VOov = eris_voov = None

    t1Tnew = numpy.zeros_like(t1T)
    t2Tnew = mycc._add_vvvv(t1T, t2T, eris, t2sym='jiba')
    time1 = log.timer_debug1('vvvv', *time0)

#** make_inter_F
    fov = fock[:nocc,nocc:].copy()
    t1Tnew += fock[nocc:,:nocc]

    foo = fock[:nocc,:nocc].copy()
    foo[numpy.diag_indices(nocc)] = 0
    foo += .5 * numpy.einsum('ia,aj->ij', fock[:nocc,nocc:], t1T)

    fvv = fock[nocc:,nocc:].copy()
    fvv[numpy.diag_indices(nvir)] = 0
    fvv -= .5 * numpy.einsum('ai,ib->ab', t1T, fock[:nocc,nocc:])

    foo_priv = numpy.zeros_like(foo)
    fov_priv = numpy.zeros_like(fov)
    fvv_priv = numpy.zeros_like(fvv)
    t1T_priv = numpy.zeros_like(t1T)

    max_memory = mycc.max_memory - lib.current_memory()[0]
    unit = nocc*nvir**2*3 + nocc**2*nvir + 1
    blksize = min(nvir, max(BLKMIN, int((max_memory*.9e6/8-t2T.size)/unit)))
    log.debug1('pass 1, max_memory %d MB,  nocc,nvir = %d,%d  blksize = %d',
               max_memory, nocc, nvir, blksize)
    nvir_pair = nvir * (nvir+1) // 2
    def load_ovvv(p0, p1):
        if p0 < p1:
            buf[:p1-p0] = eris.ovvv[:,p0:p1].transpose(1,0,2)

    fwVOov = fswap.create_dataset('wVOov', (nvir_seg,nocc,nocc,nvir), 'f8')
    wVooV = numpy.zeros((nvir_seg,nocc,nocc,nvir))
    cpu1 = log.timer('ccc      %s' % str(lib.current_memory()), *cpu1)

    buf = numpy.empty((blksize,nocc,nvir_pair))
    with lib.call_in_background(load_ovvv) as prefetch:
        load_ovvv(0, min(nvir_seg, blksize))
        for p0, p1 in lib.prange(vloc0, vloc1, blksize):
            i0, i1 = p0 - vloc0, p1 - vloc0
            eris_vovv, buf = buf[:p1-p0], numpy.empty_like(buf)
            prefetch(i1, min(nvir_seg, i1+blksize))
            cpu1 = log.timer('cc1     %d   %s' % (p0, lib.current_memory()), *cpu1)

            eris_vovv = lib.unpack_tril(eris_vovv.reshape((p1-p0)*nocc,nvir_pair))
            eris_vovv = eris_vovv.reshape(p1-p0,nocc,nvir,nvir)

            fvv_priv += 2*numpy.einsum('ck,ckab->ab', t1T[p0:p1], eris_vovv)
            fvv_priv[:,p0:p1] -= numpy.einsum('ck,bkca->ab', t1T, eris_vovv)
            cpu1 = log.timer('cc2     %d   %s' % (p0, lib.current_memory()), *cpu1)

            # Partition on index 0?
            vovv = eris_vovv.transpose(2,1,0,3)
            log.debug('vovv shape %s' % str([vovv[q0:q1].shape for q0, q1 in vlocs]))
            vovv = mpi.alltoall([vovv[q0:q1] for q0, q1 in vlocs], split_recvbuf=True)
            vovv = [x.reshape(nvir_seg,nocc,-1,nvir) for x in vovv]
            if not mycc.direct:
                tau = t2T[i0:i1] + numpy.einsum('ai,bj->abij', t1T[p0:p1], t1T)
                for task_id, tau in _rotate_tensor_block(tau):
                    tmp = lib.einsum('bkcd,cdij->bkij', vovv[task_id], tau)
                    t2Tnew -= lib.einsum('ak,bkij->baji', t1T, tmp)
                tau = tmp = None
            cpu1 = log.timer('cc3     %d   %s' % (p0, lib.current_memory()), *cpu1)

            for task_id, (q0, q1) in enumerate(comm.allgather((p0,p1))):
                wVooV -= lib.einsum('cj,bica->bija', t1T[q0:q1], vovv[task_id])
            vovv = None
            cpu1 = log.timer('cc4     %d   %s' % (p0, lib.current_memory()), *cpu1)

            theta  = t2T[i0:i1].transpose(0,2,1,3) * 2
            theta -= t2T[i0:i1].transpose(0,3,1,2)
            t1T_priv += lib.einsum('cjbi,cjba->ai', theta, eris_vovv)
            theta = None
            fwVOov[i0:i1] = lib.einsum('biac,cj->bija', eris_vovv, t1T)
            eris_voov = eris_VOov = None
            time1 = log.timer_debug1('vovv [%d:%d]'%(p0, p1), *time1)
            cpu1 = log.timer('cc5     %d   %s' % (p0, lib.current_memory()), *cpu1)
    fswap['wVooV'] = wVooV
    wVooV = None

    time1 = log.timer_debug1('ovvv', *time1)

    unit = nocc**2*nvir*7 + nocc**3 + nocc*nvir**2
    max_memory = max(0, mycc.max_memory - lib.current_memory()[0])
    blksize = min(nvir, max(BLKMIN, int((max_memory*.9e6/8-nocc**4)/unit)))
    log.debug1('pass 2, max_memory %d MB,  nocc,nvir = %d,%d  blksize = %d',
               max_memory, nocc, nvir, blksize)

    woooo = numpy.zeros((nocc,nocc,nocc,nocc))

    for p0, p1 in lib.prange(vloc0, vloc1, blksize):
        i0, i1 = p0 - vloc0, p1 - vloc0
        wVOov = fswap['wVOov'][i0:i1]
        wVooV = fswap['wVooV'][i0:i1]
        eris_ovoo = eris.ovoo[:,i0:i1]
        foo_priv += numpy.einsum('ck,kcji->ij', 2*t1T[p0:p1], eris_ovoo)
        foo_priv += numpy.einsum('ck,icjk->ij',  -t1T[p0:p1], eris_ovoo)
        tmp = lib.einsum('al,jaik->lkji', t1T[p0:p1], eris_ovoo)
        woooo += tmp + tmp.transpose(1,0,3,2)
        tmp = None
        cpu1 = log.timer('dd1     %d   %s' % (p0, lib.current_memory()), *cpu1)

        wVOov -= lib.einsum('jbik,ak->bjia', eris_ovoo, t1T)
        t2Tnew[i0:i1] += wVOov.transpose(0,3,1,2)

        wVooV += lib.einsum('kbij,ak->bija', eris_ovoo, t1T)
        eris_ovoo = None

        eris_oovv = eris.oovv[:,:,i0:i1]
        t1T_priv[p0:p1] -= numpy.einsum('bj,jiab->ai', t1T, eris_oovv)
        wVooV -= eris_oovv.transpose(2,0,1,3)

        eris_voov = eris.ovvo[:,i0:i1].transpose(1,0,3,2)
        t2Tnew[i0:i1] += eris_voov.transpose(0,3,1,2) * .5
        t1T_priv[p0:p1] += 2*numpy.einsum('bj,aijb->ai', t1T, eris_voov)

        tmp  = lib.einsum('ci,kjbc->bijk', t1T, eris_oovv)
        tmp += lib.einsum('bjkc,ci->bjik', eris_voov, t1T)
        t2Tnew[i0:i1] -= numpy.einsum('bjik,ak->baji', tmp, t1T)
        eris_oovv = tmp = None

        fov_priv[:,p0:p1] += numpy.einsum('ck,aikc->ia', t1T, eris_voov) * 2
        fov_priv[:,p0:p1] -= numpy.einsum('ck,akic->ia', t1T, eris_voov)

        tau  = numpy.einsum('ai,bj->abij', t1T[p0:p1]*.5, t1T)
        tau += t2T[i0:i1]
        theta  = tau.transpose(0,1,3,2) * 2
        theta -= tau
        fvv_priv -= lib.einsum('caij,cjib->ab', theta, eris_voov)
        foo_priv += lib.einsum('aikb,abkj->ij', eris_voov, theta)
        tau = theta = None

        wVOov += wVooV*.5  #: bjia + bija*.5
        cpu1 = log.timer('dd2     %d   %s' % (p0, lib.current_memory()), *cpu1)

        tau = t2T[i0:i1] + numpy.einsum('ai,bj->abij', t1T[p0:p1], t1T)
        woooo += lib.einsum('abij,aklb->ijkl', tau, eris_voov)
        tau = None

        wVooV += fswap['wVooV1'][i0:i1]
        fswap['wVooV1'][i0:i1] = wVooV
        cpu1 = log.timer('dd3     %d   %s' % (p0, lib.current_memory()), *cpu1)
        wVOov += fswap['wVOov1'][i0:i1]
        fswap['wVOov1'][i0:i1] = wVOov
        cpu1 = log.timer('dd4     %d   %s' % (p0, lib.current_memory()), *cpu1)
        eris_VOov = wVOov = wVooV = None
        time1 = log.timer_debug1('voov [%d:%d]'%(p0, p1), *time1)

    wVooV = numpy.asarray(fswap['wVooV1'])
    for task_id, wVooV, p0, p1 in _rotate_vir_block(wVooV):
        cpu1 = log.timer('eee0     %d %s' % (task_id, lib.current_memory()), *cpu1)
        tmp = lib.einsum('ackj,ckib->ajbi', t2T[:,p0:p1], wVooV)
        t2Tnew += tmp.transpose(0,2,3,1)
        t2Tnew += tmp.transpose(0,2,1,3) * .5
        cpu1 = log.timer('eee1     %d %s' % (task_id, lib.current_memory()), *cpu1)
    wVooV = tmp = None

    wVOov = numpy.asarray(fswap['wVOov1'])
    theta  = t2T * 2
    theta -= t2T.transpose(0,1,3,2)
    for task_id, wVOov, p0, p1 in _rotate_vir_block(wVOov):
        cpu1 = log.timer('fff0     %d %s' % (task_id, lib.current_memory()), *cpu1)
        t2Tnew += lib.einsum('acik,ckjb->abij', theta[:,p0:p1], wVOov)
        cpu1 = log.timer('fff1     %d %s' % (task_id, lib.current_memory()), *cpu1)
    wVOov = theta = None
    fwVOov = fswap = None

    foo += mpi.allreduce(foo_priv)
    fov += mpi.allreduce(fov_priv)
    fvv += mpi.allreduce(fvv_priv)

    theta = t2T.transpose(0,1,3,2) * 2 - t2T
    t1T_priv[vloc0:vloc1] += numpy.einsum('jb,abji->ai', fov, theta)
    ovoo = numpy.asarray(eris.ovoo)
    for task_id, ovoo, p0, p1 in _rotate_vir_block(ovoo):
        cpu1 = log.timer('ggg0     %d %s' % (task_id, lib.current_memory()), *cpu1)
        t1T_priv[vloc0:vloc1] -= lib.einsum('jbki,abjk->ai', ovoo, theta[:,p0:p1])
        cpu1 = log.timer('ggg1     %d %s' % (task_id, lib.current_memory()), *cpu1)
    theta = ovoo = None

    woooo = mpi.allreduce(woooo)
    woooo += numpy.asarray(eris.oooo).transpose(0,2,1,3)
    tau = t2T + numpy.einsum('ai,bj->abij', t1T[vloc0:vloc1], t1T)
    t2Tnew += .5 * lib.einsum('abkl,ijkl->abij', tau, woooo)
    tau = woooo = None

    t1Tnew += mpi.allreduce(t1T_priv)

    ft_ij = foo + numpy.einsum('aj,ia->ij', .5*t1T, fov)
    ft_ab = fvv - numpy.einsum('ai,ib->ab', .5*t1T, fov)
    t2Tnew += lib.einsum('acij,bc->abij', t2T, ft_ab)
    t2Tnew -= lib.einsum('ki,abkj->abij', ft_ij, t2T)
    cpu1 = log.timer('hhh        %s' % str(lib.current_memory()), *cpu1)

    mo_e = fock.diagonal()
    eia = mo_e[:nocc,None] - mo_e[None,nocc:]
    t1Tnew += numpy.einsum('bi,ab->ai', t1T, fvv)
    t1Tnew -= numpy.einsum('aj,ji->ai', t1T, foo)
    t1Tnew /= eia.T

    t2tmp = mpi.alltoall([t2Tnew[:,p0:p1] for p0,p1 in vlocs],
                         split_recvbuf=True)
    cpu1 = log.timer('iii        %s' % str(lib.current_memory()), *cpu1)
    for task_id, (p0, p1) in enumerate(vlocs):
        cpu1 = log.timer('iii0     %d  %s' % (task_id, lib.current_memory()), *cpu1)
        tmp = t2tmp[task_id].reshape(p1-p0,nvir_seg,nocc,nocc)
        t2Tnew[:,p0:p1] += tmp.transpose(1,0,3,2)
        cpu1 = log.timer('iii1     %d  %s' % (task_id, lib.current_memory()), *cpu1)

    for i in range(vloc0, vloc1):
        t2Tnew[i-vloc0] /= lib.direct_sum('i+jb->bij', eia[:,i], eia)

    cpu1 = log.timer('jjj          %s' % str(lib.current_memory()), *cpu1)
    time0 = log.timer_debug1('update t1 t2', *time0)
    return t1Tnew.T, t2Tnew.transpose(2,3,0,1)

def _add_vvvv(mycc, t1T, t2T, eris, out=None, with_ovvv=None, t2sym=None):
    '''t2sym: whether t2 has the symmetry t2[ijab]==t2[jiba] or
    t2[ijab]==-t2[jiab] or t2[ijab]==-t2[jiba]
    '''
    if t2sym == 'jiba':
        nvir_seg, nvir, nocc = t2T.shape[:3]
        Ht2tril = _add_vvvv_tril(mycc, t1T, t2T, eris, with_ovvv=with_ovvv)
        Ht2 = numpy.zeros_like(t2T)
        Ht2 = lib.unpack_tril(Ht2tril.reshape(nvir_seg*nvir,nocc*(nocc+1)//2),
                              filltriu=lib.PLAIN, out=Ht2).reshape(t2T.shape)
        oidx = numpy.arange(nocc)
        Ht2[:,:,oidx,oidx] *= .5
    else:
        Ht2 = _add_vvvv_full(mycc, t1T, t2T, eris, out, with_ovvv)
        Ht2 *= .5
    return Ht2

def _add_vvvv_tril(mycc, t1T, t2T, eris, out=None, with_ovvv=None):
    '''Ht2 = numpy.einsum('ijcd,acdb->ijab', t2, vvvv)
    Using symmetry t2[ijab] = t2[jiba] and Ht2[ijab] = Ht2[jiba], compute the
    lower triangular part of  Ht2
    '''
    time0 = time.clock(), time.time()
    log = logger.Logger(mycc.stdout, mycc.verbose)
    if with_ovvv is None:
        with_ovvv = mycc.direct
    nvir_seg, nvir, nocc = t2T.shape[:3]
    vloc0, vloc1 = _task_location(nvir, rank)
    nocc2 = nocc*(nocc+1)//2
    if t1T is None:
        tau = lib.pack_tril(t2T.reshape(nvir_seg*nvir,nocc,nocc))
    else:
        tau = t2T + numpy.einsum('ai,bj->abij', t1T[vloc0:vloc1], t1T)
        tau = lib.pack_tril(tau.reshape(nvir_seg*nvir,nocc,nocc))
    tau = tau.reshape(nvir_seg,nvir,nocc2)
    max_memory = max(0, mycc.max_memory - lib.current_memory()[0])

    if mycc.direct:   # AO-direct CCSD
        mo = getattr(eris, 'mo_coeff', None)
        if mo is None:  # If eris does not have the attribute mo_coeff
            mo = _mo_without_core(mycc, mycc.mo_coeff)

        tau_shape = tau.shape
        ao_loc = mycc.mol.ao_loc_nr()
        orbv = mo[:,nocc:]
        nao, nvir = orbv.shape

        ntasks = mpi.pool.size
        task_sh_locs = lib.misc._balanced_partition(ao_loc, ntasks)
        ao_loc0 = ao_loc[task_sh_locs[rank  ]]
        ao_loc1 = ao_loc[task_sh_locs[rank+1]]

        tau = lib.einsum('pb,abx->apx', orbv, tau)
        tau_priv = numpy.zeros((ao_loc1-ao_loc0,nao,nocc2))
        for task_id, tau in _rotate_tensor_block(tau):
            loc0, loc1 = _task_location(nvir, task_id)
            tau_priv += lib.einsum('pa,abx->pbx', orbv[ao_loc0:ao_loc1,loc0:loc1], tau)
        tau = None
        time1 = log.timer_debug1('vvvv-tau mo2ao', *time0)

        buf = _contract_vvvv_t2(mycc, None, tau_priv, task_sh_locs, None,
                                max_memory, log)
        buf = buf_ao = buf.reshape(tau_priv.shape)
        tau_priv = None
        time1 = log.timer_debug1('vvvv-tau contraction', *time1)

        buf = lib.einsum('apx,pb->abx', buf, orbv)
        Ht2tril = numpy.ndarray((nvir_seg,nvir,nocc2), buffer=out)
        Ht2tril[:] = 0
        for task_id, buf in _rotate_tensor_block(buf):
            ao_loc0 = ao_loc[task_sh_locs[task_id  ]]
            ao_loc1 = ao_loc[task_sh_locs[task_id+1]]
            Ht2tril += lib.einsum('pa,pbx->abx', orbv[ao_loc0:ao_loc1,vloc0:vloc1], buf)
        time1 = log.timer_debug1('vvvv-tau ao2mo', *time1)

        if with_ovvv:
            #: tmp = numpy.einsum('ijcd,ak,kdcb->ijba', tau, t1T, eris.ovvv)
            #: t2new -= tmp + tmp.transpose(1,0,3,2)
            orbo = mo[:,:nocc]
            buf = lib.einsum('apx,pi->axi', buf_ao, orbo)
            tmp = numpy.zeros((nvir_seg,nocc2,nocc))
            for task_id, buf in _rotate_tensor_block(buf):
                ao_loc0 = ao_loc[task_sh_locs[task_id  ]]
                ao_loc1 = ao_loc[task_sh_locs[task_id+1]]
                tmp += lib.einsum('pa,pxi->axi', orbv[ao_loc0:ao_loc1,vloc0:vloc1], buf)
            Ht2tril -= lib.einsum('axi,bi->abx', tmp, t1T)
            tmp = buf = None

            t1_ao = numpy.dot(orbo, t1T[vloc0:vloc1].T)
            buf = lib.einsum('apx,pb->abx', buf_ao, orbv)
            for task_id, buf in _rotate_tensor_block(buf):
                ao_loc0 = ao_loc[task_sh_locs[task_id  ]]
                ao_loc1 = ao_loc[task_sh_locs[task_id+1]]
                Ht2tril -= lib.einsum('pa,pbx->abx', t1_ao[ao_loc0:ao_loc1], buf)
        time1 = log.timer_debug1('vvvv-tau ao2mo', *time0)
    else:
        raise NotImplementedError
    return Ht2tril

def _add_vvvv_full(mycc, t1T, t2T, eris, out=None, with_ovvv=False):
    '''Ht2 = numpy.einsum('ijcd,acdb->ijab', t2, vvvv)
    without using symmetry t2[ijab] = t2[jiba] in t2 or Ht2
    '''
    time0 = time.clock(), time.time()
    log = logger.Logger(mycc.stdout, mycc.verbose)

    nvir_seg, nvir, nocc = t2T.shape[:3]
    vloc0, vloc1 = _task_location(nvir, rank)
    nocc2 = nocc*(nocc+1)//2
    if t1T is None:
        tau = lib.pack_tril(t2T.reshape(nvir_seg*nvir,nocc2))
    else:
        tau = t2T + numpy.einsum('ai,bj->abij', t1T[vloc0:vloc1], t1T)
        tau = lib.pack_tril(tau.reshape(nvir_seg*nvir,nocc2))
    tau = tau.reshape(nvir_seg,nvir,nocc2)
    max_memory = max(0, mycc.max_memory - lib.current_memory()[0])

    if mycc.direct:   # AO-direct CCSD
        if with_ovvv:
            raise NotImplementedError
        mo = getattr(eris, 'mo_coeff', None)
        if mo is None:  # If eris does not have the attribute mo_coeff
            mo = _mo_without_core(mycc, mycc.mo_coeff)

        ao_loc = mycc.mol.ao_loc_nr()
        nao, nmo = mo.shape
        ntasks = mpi.pool.size
        task_sh_locs = lib.misc._balanced_partition(ao_loc, ntasks)
        ao_loc0 = ao_loc[task_sh_locs[rank  ]]
        ao_loc1 = ao_loc[task_sh_locs[rank+1]]

        orbv = mo[:,nocc:]
        tau = lib.einsum('abij,pb->apij', tau, orbv)
        tau_priv = numpy.zeros((ao_loc1-ao_loc0,nao,nocc,nocc))
        for task_id, tau in _rotate_tensor_block(tau):
            loc0, loc1 = _task_location(nvir, task_id)
            tau_priv += lib.einsum('pa,abij->pbij', orbv[ao_loc0:ao_loc1,loc0:loc1], tau)
        tau = None
        time1 = log.timer_debug1('vvvv-tau mo2ao', *time0)

        buf = _contract_vvvv_t2(mycc, None, tau_priv, task_sh_locs, None,
                                max_memory, log)
        buf = buf.reshape(tau_priv.shape)
        tau_priv = None
        time1 = log.timer_debug1('vvvv-tau contraction', *time1)

        buf = lib.einsum('apij,pb->abij', buf, orbv)
        Ht2 = numpy.ndarray(t2T.shape, buffer=out)
        Ht2[:] = 0
        for task_id, buf in _rotate_tensor_block(buf):
            ao_loc0 = ao_loc[task_sh_locs[task_id  ]]
            ao_loc1 = ao_loc[task_sh_locs[task_id+1]]
            Ht2 += lib.einsum('pa,pbij->abij', orbv[ao_loc0:ao_loc1,vloc0:vloc1], buf)

        time1 = log.timer_debug1('vvvv-tau ao2mo', *time1)
    else:
        raise NotImplementedError
    return Ht2.reshape(t2T.shape)

def _task_location(n, task=rank):
    ntasks = mpi.pool.size
    seg_size = (n + ntasks - 1) // ntasks
    loc0 = seg_size * task
    loc1 = min(n, loc0 + seg_size)
    return loc0, loc1

ASYNC = True
if ASYNC:
    def _rotate_tensor_block(buf):
        ntasks = mpi.pool.size
        tasks = list(range(ntasks))
        tasks = tasks[rank:] + tasks[:rank]

        buf_prefetch = [None]
        def rotate():
            buf_prefetch[0] = mpi.rotate(buf, blocking=False)
# DO NOT ThreadWithReturnValue, the return value of mpi.rotate is too large
# for Queue module.
        handler = lib.ThreadWithTraceBack(target=rotate, args=())
        handler.start()
        for k, task in enumerate(tasks):
            if task != rank:
                handler.join()
                buf = buf_prefetch[0]
                if k + 1 < ntasks:
                    handler = lib.ThreadWithTraceBack(target=rotate, args=())
                    handler.start()
            yield task, buf
else:
    def _rotate_tensor_block1(buf):
        ntasks = mpi.pool.size
        tasks = list(range(ntasks))
        tasks = tasks[rank:] + tasks[:rank]
        for task in tasks:
            if task != rank:
                buf = mpi.rotate(buf)
            yield task, buf


def _contract_vvvv_t2(mycc, vvvv, t2T, task_locs, out=None, max_memory=MEMORYMIN,
                      verbose=None):
    '''Ht2 = numpy.einsum('ijcd,acbd->ijab', t2, vvvv)
    where vvvv has to be real and has the 4-fold permutation symmetry

    Args:
        vvvv : None or integral object
            if vvvv is None, contract t2 to AO-integrals using AO-direct algorithm
    '''
    time0 = time.clock(), time.time()
    mol = mycc.mol
    log = logger.new_logger(mycc, verbose)

    if callable(t2T):
        t2T = t2T()
    assert(t2T.dtype == numpy.double)
    nvira, nvirb = t2T.shape[:2]
    nvir2 = nvira * nvirb
    t2T = t2T.reshape(nvira,nvirb,-1)
    nocc2 = t2T.shape[2]
    Ht2 = numpy.ndarray(t2T.shape, dtype=t2T.dtype, buffer=out)
    Ht2[:] = 0

    _dgemm = lib.numpy_helper._dgemm
    def contract_blk_(Ht2, t2T, eri, i0, i1, j0, j1):
        ic = i1 - i0
        jc = j1 - j0
        #:Ht2[j0:j1] += numpy.einsum('efx,efab->abx', t2T[i0:i1], eri)
        _dgemm('T', 'N', jc*nvirb, nocc2, ic*nvirb,
               eri.reshape(ic*nvirb,jc*nvirb), t2T.reshape(-1,nocc2),
               Ht2.reshape(nvir2,nocc2), 1, 1, 0, i0*nvirb*nocc2, j0*nvirb*nocc2)

    if vvvv is None:   # AO-direct CCSD
        ao_loc = mol.ao_loc_nr()
        intor = mol._add_suffix('int2e')
        ao2mopt = _ao2mo.AO2MOpt(mol, intor, 'CVHFnr_schwarz_cond',
                                 'CVHFsetnr_direct_scf')
        max_words = max(0, max_memory*.95e6/8-t2T.size*2)
        blksize = max(BLKMIN, numpy.sqrt(max_words/nvirb**2/2))
        fint = gto.moleintor.getints4c
        fload = ccsd._ccsd.libcc.CCload_eri

        ntasks = mpi.pool.size
        task_sh_locs = task_locs
        sh_ranges_tasks = []
        for task in range(ntasks):
            sh0 = task_sh_locs[task]
            sh1 = task_sh_locs[task+1]
            sh_ranges = ao2mo.outcore.balance_partition(ao_loc, blksize, sh0, sh1)
            sh_ranges_tasks.append(sh_ranges)

        blksize = max(max(x[2] for x in sh_ranges)
                      for sh_ranges in sh_ranges_tasks)
        eribuf = numpy.empty((blksize,blksize,nvirb,nvirb))
        loadbuf = numpy.empty((blksize,blksize,nvirb,nvirb))

        ao_sh_ranges = sh_ranges_tasks[rank]
        ao_sh0 = task_sh_locs[rank]
        ao_sh1 = task_sh_locs[rank+1]
        ao_offset = ao_loc[ao_sh0]
        assert(nvira == ao_loc[ao_sh1] - ao_loc[ao_sh0])

        for task_id, t2T in _rotate_tensor_block(t2T):
            sh_ranges = sh_ranges_tasks[task_id]
            sh0 = task_sh_locs[task_id]
            cur_offset = ao_loc[sh0]

            for ish0, ish1, ni in sh_ranges:
                for jsh0, jsh1, nj in ao_sh_ranges:
                    eri = fint(intor, mol._atm, mol._bas, mol._env,
                               shls_slice=(ish0,ish1,jsh0,jsh1), aosym='s2kl',
                               ao_loc=ao_loc, cintopt=ao2mopt._cintopt, out=eribuf)
                    i0, i1 = ao_loc[ish0] - cur_offset, ao_loc[ish1] - cur_offset
                    j0, j1 = ao_loc[jsh0] - ao_offset , ao_loc[jsh1] - ao_offset
                    tmp = numpy.ndarray((i1-i0,nvirb,j1-j0,nvirb), buffer=loadbuf)
                    fload(tmp.ctypes.data_as(ctypes.c_void_p),
                          eri.ctypes.data_as(ctypes.c_void_p),
                          (ctypes.c_int*4)(i0, i1, j0, j1),
                          ctypes.c_int(nvirb))
                    contract_blk_(Ht2, t2T, tmp, i0, i1, j0, j1)
                    time0 = log.timer_debug1('AO-vvvv [%d:%d,%d:%d]' %
                                             (ish0,ish1,jsh0,jsh1), *time0)
    else:
        raise NotImplementedError
    return Ht2

def amplitudes_to_vector(t1, t2, out=None):
    t2T = t2.transpose(2,3,0,1)
    nvir_seg, nvir, nocc = t2T.shape[:3]
    if rank == 0:
        t1T = t1.T
        nov = nocc * nvir
        nocc2 = nocc*(nocc+1)//2
        size = nov + nvir_seg*nvir*nocc2
        vector = numpy.ndarray(size, t1.dtype, buffer=out)
        vector[:nov] = t1T.ravel()
        lib.pack_tril(t2T.reshape(nvir_seg*nvir,nocc,nocc), out=vector[nov:])
    else:
        vector = lib.pack_tril(t2T.reshape(nvir_seg*nvir,nocc,nocc))
    return vector

def vector_to_amplitudes(vector, nmo, nocc):
    nvir = nmo - nocc
    nov = nocc * nvir
    nocc2 = nocc*(nocc+1)//2
    vlocs = [_task_location(nvir, task_id) for task_id in range(mpi.pool.size)]
    vloc0, vloc1 = vlocs[rank]
    nvir_seg = vloc1 - vloc0

    if rank == 0:
        t1T = vector[:nov].copy().reshape((nvir,nocc))
        mpi.bcast(t1T)
        t2tril = vector[nov:].reshape(nvir_seg,nvir,nocc2)
    else:
        t1T = mpi.bcast(None)
        t2tril = vector.reshape(nvir_seg,nvir,nocc2)

    t2T = lib.unpack_tril(t2tril.reshape(nvir_seg*nvir,nocc2), filltriu=lib.PLAIN)
    t2T = t2T.reshape(nvir_seg,nvir,nocc,nocc)
    t2tmp = mpi.alltoall([t2tril[:,p0:p1] for p0,p1 in vlocs], split_recvbuf=True)
    idx,idy = numpy.tril_indices(nocc)
    for task_id, (p0, p1) in enumerate(vlocs):
        tmp = t2tmp[task_id].reshape(nvir_seg,p1-p0,nocc2)
        t2T[:,p0:p1,idy,idx] = tmp.transpose(1,0,2)
    return t1T.T, t2T.transpose(2,3,0,1)


@mpi.parallel_call
def init_amps(mycc, eris=None):
    eris = getattr(mycc, '_eris', None)
    if eris is None:
        mycc.ao2mo()
        eris = mycc._eris

    time0 = time.clock(), time.time()
    mo_e = eris.fock.diagonal()
    nocc = mycc.nocc
    nvir = mo_e.size - nocc
    eia = mo_e[:nocc,None] - mo_e[None,nocc:]
    t1T = eris.fock[nocc:,:nocc] / eia.T
    loc0, loc1 = _task_location(nvir)

    t2T = numpy.empty((loc1-loc0,nvir,nocc,nocc))
    max_memory = mycc.max_memory - lib.current_memory()[0]
    blksize = int(min(nvir, max(BLKMIN, max_memory*.3e6/8/(nocc**2*nvir+1))))
    emp2 = 0
    for p0, p1 in lib.prange(0, loc1-loc0, blksize):
        eris_ovov = eris.ovov[:,p0:p1]
        t2T[p0:p1] = (eris_ovov.transpose(1,3,0,2) /
                      lib.direct_sum('ia,jb->abij', eia[:,p0+loc0:p1+loc0], eia))
        emp2 += 2 * numpy.einsum('abij,iajb', t2T[p0:p1], eris_ovov)
        emp2 -=     numpy.einsum('abji,iajb', t2T[p0:p1], eris_ovov)

    mycc.emp2 = comm.allreduce(emp2)
    logger.info(mycc, 'Init t2, MP2 energy = %.15g', mycc.emp2)
    logger.timer(mycc, 'init mp2', *time0)
    return mycc.emp2, t1T.T, t2T.transpose(2,3,0,1)

@mpi.parallel_call
def energy(mycc, t1=None, t2=None, eris=None):
    '''CCSD correlation energy'''
    if t1 is None: t1 = mycc.t1
    if t2 is None: t2 = mycc.t2
    eris = getattr(mycc, '_eris', None)
    if eris is None:
        mycc.ao2mo()
        eris = mycc._eris

    nocc, nvir = t1.shape
    t2T = t2.transpose(2,3,0,1)
    fock = eris.fock
    loc0, loc1 = _task_location(nvir)
    e = numpy.einsum('ia,ia', fock[:nocc,nocc:], t1) * 2
    max_memory = mycc.max_memory - lib.current_memory()[0]
    blksize = int(min(nvir, max(BLKMIN, max_memory*.3e6/8/(nocc**2*nvir+1))))
    for p0, p1 in lib.prange(0, loc1-loc0, blksize):
        eris_ovov = eris.ovov[:,p0:p1]
        tau = t2T[p0:p1] + numpy.einsum('ia,jb->abij', t1[:,p0+loc0:p1+loc0], t1)
        e += 2 * numpy.einsum('abij,iajb', tau, eris_ovov)
        e -=     numpy.einsum('abji,iajb', tau, eris_ovov)
    e = comm.allreduce(e)

    if rank == 0 and abs(e.imag) > 1e-4:
        logger.warn(mycc, 'Non-zero imaginary part found in CCSD energy %s', e)
    return e

@mpi.parallel_call
def distribute_t2_(mycc, t2=None):
    '''Distribute the entire t2 amplitudes tensor (nocc,nocc,nvir,nvir) to
    different processes
    '''
    if rank == 0:
        if t2 is None: t2 = mycc.t2
        nocc = t2.shape[0]
        nvir = t2.shape[2]
        t2T = t2.transpose(2,3,0,1)
        t2_all = []
        for task_id in range(mpi.pool.size):
            loc0, loc1 = _task_location(nvir, task_id)
            t2_all.append(t2T[loc0:loc1])
        t2T = mpi.comm.scatter(t2_all)
    else:
        t2T = mpi.comm.scatter(None)
    mycc.t2 = t2T.transpose(2,3,0,1)
    return mycc.t2

def _diff_norm(mycc, t1new, t2new, t1, t2):
    norm2 = comm.allreduce(numpy.linalg.norm(t2new - t2))
    norm1 = numpy.linalg.norm(t1new - t1)
    return (norm1**2 + norm2**2)**.5

# Temporarily place here.  Move it to mpi_scf module in the future
def _pack_scf(mf):
    mfdic = {'verbose'    : mf.verbose,
             'max_memory' : mf.max_memory,
             'mo_energy'  : mf.mo_energy,
             'mo_coeff'   : mf.mo_coeff,
             'mo_occ'     : mf.mo_occ,
             'e_tot'      : mf.e_tot}
    return mfdic

def _init_ccsd(ccsd_obj):
    from pyscf import gto
    from mpi4pyscf.tools import mpi
    from mpi4pyscf.cc import ccsd
    if mpi.rank == 0:
        mpi.comm.bcast((ccsd_obj.mol.dumps(), ccsd_obj.pack()))
    else:
        ccsd_obj = ccsd.CCSD.__new__(ccsd.CCSD)
        mol, cc_attr = mpi.comm.bcast(None)
        ccsd_obj.mol = gto.mole.loads(mol)
        ccsd_obj.unpack_(cc_attr)
    if 0:  # If also to initialize cc._scf object
        if mpi.rank == 0:
            mpi.comm.bcast((ccsd_obj._scf.__class__, ccsd._pack_scf(ccsd_obj._scf)))
        else:
            mf_cls, mf_attr = mpi.comm.bcast(None)
            ccsd_obj._scf = mf_cls(ccsd_obj.mol)
            ccsd_obj._scf.__dict__.update(mf_attr)

    key = id(ccsd_obj)
    mpi._registry[key] = ccsd_obj
    regs = mpi.comm.gather(key)
    return regs

class CCSD(ccsd.CCSD):
    def __init__(self, mf, frozen=0, mo_coeff=None, mo_occ=None):
        ccsd.CCSD.__init__(self, mf, frozen, mo_coeff, mo_occ)
        self.direct = True
        regs = mpi.pool.apply(_init_ccsd, self, (None,))
        self._reg_procs = regs

    def pack(self):
        return {'verbose'   : self.verbose,
                'max_memory': self.max_memory,
                'frozen'    : self.frozen,
                'mo_coeff'  : self.mo_coeff,
                'mo_occ'    : self.mo_occ,
                '_nocc'     : self._nocc,
                '_nmo'      : self._nmo,
                'direct'    : self.direct}
    def unpack_(self, ccdic):
        self.__dict__.update(ccdic)
        return self

    def dump_flags(self):
        if rank == 0:
            ccsd.CCSD.dump_flags(self)
        return self
    def sanity_check(self):
        if rank == 0:
            ccsd.CCSD.sanity_check(self)
        return self

    init_amps = init_amps
    energy = energy
    _add_vvvv = _add_vvvv
    update_amps = update_amps

    def kernel(self, t1=None, t2=None, eris=None):
        return self.ccsd(t1, t2, eris)
    def ccsd(self, t1=None, t2=None, eris=None):
        assert(self.mo_coeff is not None)
        assert(self.mo_occ is not None)
        if self.verbose >= logger.WARN:
            self.check_sanity()
        self.dump_flags()

        self.converged, self.e_corr, self.t1, self.t2 = \
                kernel(self, eris, t1, t2, max_cycle=self.max_cycle,
                       tol=self.conv_tol, tolnormt=self.conv_tol_normt,
                       verbose=self.verbose)
        if rank == 0:
            self._finalize()
        return self.e_corr, self.t1, self.t2

    def ao2mo(self, mo_coeff=None):
        _make_eris_outcore(self, mo_coeff)
        return 'Done'

    def run_diis(self, t1, t2, istep, normt, de, adiis):
        if (adiis and
            istep >= self.diis_start_cycle and
            abs(de) < self.diis_start_energy_diff):
            vec = self.amplitudes_to_vector(t1, t2)
            t1, t2 = self.vector_to_amplitudes(adiis.update(vec))
            logger.debug1(self, 'DIIS for step %d', istep)
        return t1, t2

    def amplitudes_to_vector(self, t1, t2, out=None):
        return amplitudes_to_vector(t1, t2, out)

    def vector_to_amplitudes(self, vec, nmo=None, nocc=None):
        if nocc is None: nocc = self.nocc
        if nmo is None: nmo = self.nmo
        return vector_to_amplitudes(vec, nmo, nocc)

CC = RCCSD = CCSD

@mpi.parallel_call
def _make_eris_outcore(mycc, mo_coeff=None):
    cput0 = (time.clock(), time.time())
    log = logger.Logger(mycc.stdout, mycc.verbose)
    eris = ccsd._ChemistsERIs()
    if rank == 0:
        eris._common_init_(mycc, mo_coeff)
        comm.bcast((eris.mo_coeff, eris.fock, eris.nocc))
    else:
        eris.mol = mycc.mol
        eris.mo_coeff, eris.fock, eris.nocc = comm.bcast(None)

    mol = mycc.mol
    mo_coeff = numpy.asarray(eris.mo_coeff, order='F')
    nocc = eris.nocc
    nao, nmo = mo_coeff.shape
    nvir = nmo - nocc
    orbo = mo_coeff[:,:nocc]
    orbv = mo_coeff[:,nocc:]
    nvpair = nvir * (nvir+1) // 2
    v0, v1 = _task_location(nvir)

    eris.feri1 = lib.H5TmpFile()
    eris.oooo = eris.feri1.create_dataset('oooo', (nocc,nocc,nocc,nocc), 'f8')
    eris.oovv = eris.feri1.create_dataset('oovv', (nocc,nocc,v1-v0,nvir), 'f8', chunks=(nocc,nocc,1,nvir))
    eris.ovoo = eris.feri1.create_dataset('ovoo', (nocc,v1-v0,nocc,nocc), 'f8', chunks=(nocc,1,nocc,nocc))
    eris.ovvo = eris.feri1.create_dataset('ovvo', (nocc,v1-v0,nvir,nocc), 'f8', chunks=(nocc,1,nvir,nocc))
    eris.ovov = eris.feri1.create_dataset('ovov', (nocc,v1-v0,nocc,nvir), 'f8', chunks=(nocc,1,nocc,nvir))
    eris.ovvv = eris.feri1.create_dataset('ovvv', (nocc,v1-v0,nvpair), 'f8', chunks=(nocc,1,nvpair))
    #eris.vvvo = eris.feri1.create_dataset('vvvo', (v1-v0,nvir,nvir,nocc), 'f8', chunks=(v1-v0,nvir,1,nocc))
    assert(mycc.direct)

    oovv = numpy.empty((nocc,nocc,nvir,nvir))
    def save_occ_frac(p0, p1, eri):
        eri = eri.reshape(p1-p0,nocc,nmo,nmo)
        eris.oooo[p0:p1] = eri[:,:,:nocc,:nocc]
        eris.oovv[p0:p1] = eri[:,:,nocc+v0:nocc+v1,nocc:]

    def save_vir_frac(p0, p1, eri):
        eri = eri.reshape(p1-p0,nocc,nmo,nmo)
        eris.ovoo[:,p0:p1] = eri[:,:,:nocc,:nocc].transpose(1,0,2,3)
        eris.ovvo[:,p0:p1] = eri[:,:,nocc:,:nocc].transpose(1,0,2,3)
        eris.ovov[:,p0:p1] = eri[:,:,:nocc,nocc:].transpose(1,0,2,3)
        vvv = lib.pack_tril(eri[:,:,nocc:,nocc:].reshape((p1-p0)*nocc,nvir,nvir))
        eris.ovvv[:,p0:p1] = vvv.reshape(p1-p0,nocc,nvpair).transpose(1,0,2)

    cput1 = time.clock(), time.time()

    fswap = lib.H5TmpFile()
    max_memory = max(MEMORYMIN, mycc.max_memory-lib.current_memory()[0])
    int2e = mol._add_suffix('int2e')
    orbov = numpy.hstack((orbo, orbv[:,v0:v1]))
    ao2mo.outcore.half_e1(mol, (orbov,orbo), fswap, int2e,
                          's4', 1, max_memory, verbose=log)

    ao_loc = mol.ao_loc_nr()
    nao_pair = nao * (nao+1) // 2
    blksize = int(min(8e9,max_memory*.5e6)/8/(nao_pair+nmo**2)/nocc)
    blksize = min(nmo, max(BLKMIN, blksize))
    fload = ao2mo.outcore._load_from_h5g

    buf = numpy.empty((blksize*nocc,nao_pair))
    buf_prefetch = numpy.empty_like(buf)
    def prefetch(p0, p1, rowmax):
        p0, p1 = p1, min(rowmax, p1+blksize)
        if p0 < p1:
            fload(fswap['0'], p0*nocc, p1*nocc, buf_prefetch)

    outbuf = numpy.empty((blksize*nocc,nmo**2))
    with lib.call_in_background(prefetch) as bprefetch:
        fload(fswap['0'], 0, min(nocc,blksize)*nocc, buf_prefetch)
        for p0, p1 in lib.prange(0, nocc, blksize):
            nrow = (p1 - p0) * nocc
            buf, buf_prefetch = buf_prefetch, buf
            bprefetch(p0, p1, nocc)
            dat = ao2mo._ao2mo.nr_e2(buf[:nrow], mo_coeff, (0,nmo,0,nmo),
                                     's4', 's1', out=outbuf, ao_loc=ao_loc)
            save_occ_frac(p0, p1, dat)

        norb_max = nocc + v1 - v0
        fload(fswap['0'], nocc**2, min(nocc+blksize,norb_max)*nocc, buf_prefetch)
        for p0, p1 in lib.prange(0, v1-v0, blksize):
            nrow = (p1 - p0) * nocc
            buf, buf_prefetch = buf_prefetch, buf
            bprefetch(nocc+p0, nocc+p1, norb_max)
            dat = ao2mo._ao2mo.nr_e2(buf[:nrow], mo_coeff, (0,nmo,0,nmo),
                                     's4', 's1', out=outbuf, ao_loc=ao_loc)
            save_vir_frac(p0, p1, dat)

    cput1 = log.timer_debug1('transforming oppp', *cput1)
    log.timer('CCSD integral transformation', *cput0)
    mycc._eris = eris
    return eris

def _sync_(mycc):
    return mycc.unpack_(comm.bcast(mycc.pack()))


if __name__ == '__main__':
    from pyscf import gto
    from pyscf import scf
    from pyscf import cc

    mol = gto.Mole()
    mol.atom = [
        [2 , (0. , 0.     , 0.)],
        [1 , (0. , -0.757 , 0.587)],
        [1 , (0. , 0.757  , 0.587)]]
    mol.basis = '6-31g'
    mol.build()
    mf = scf.RHF(mol)
    nao = mol.nao_nr()
    numpy.random.seed(1)
    mf.mo_coeff = numpy.random.random((nao,nao)) - 0.5
    mf.mo_occ = numpy.zeros(nao)
    nocc = mol.nelectron // 2
    nvir = nao - nocc
    mf.mo_occ[:mol.nelectron//2] = 2

    mycc = cc.CCSD(mf)
    mycc.direct = True
    eris = mycc.ao2mo(mf.mo_coeff)

    emp2, v1, v2 = mycc.init_amps(eris)
    print(lib.finger(v1) - 0.20852878109950079)
    print(lib.finger(v2) - 0.21333574169417541)
    print(emp2 - -0.12037888088751542)

    t1 = numpy.random.random((nocc,nvir))
    t2 = numpy.random.random((nocc,nocc,nvir,nvir))
    t2 = t2 + t2.transpose(1,0,3,2)
    v1, v2 = mycc.update_amps(t1, t2, eris)
    print(lib.finger(v1) - 9.6029949445427079)
    print(lib.finger(v2) - 4.5308876217231813)