#!/usr/bin/env python
#
# Author: Qiming Sun <osirpt.sun@gmail.com>
#

import time
import numpy
from pyscf import lib
from pyscf import ao2mo
from pyscf.ao2mo import _ao2mo
from pyscf.pbc import tools
from pyscf.pbc.df.df_jk import zdotNN, zdotCN, zdotNC

from mpi4pyscf.lib import logger
from mpi4pyscf.tools import mpi
from mpi4pyscf.pbc.df.df_jk import _sync_mydf

comm = mpi.comm
rank = mpi.rank


def _get_eri(mydf, kpts=None, compact=True):
    if mydf._cderi is None:
        mydf._build()
    mydf = _sync_mydf(mydf)
    cell = mydf.cell
    if kpts is None:
        kptijkl = numpy.zeros((4,3))
    elif numpy.shape(kpts) == (3,):
        kptijkl = numpy.vstack([kpts]*4)
    else:
        kptijkl = numpy.reshape(kpts, (4,3))

    kpti, kptj, kptk, kptl = kptijkl
    auxcell = mydf.auxcell
    nao = cell.nao_nr()
    naux = auxcell.nao_nr()
    nao_pair = nao * (nao+1) // 2
    max_memory = max(2000, (mydf.max_memory - lib.current_memory()[0]) * .8)

####################
# gamma point, the integral is real and with s4 symmetry
    if abs(kptijkl).sum() < 1e-9:
        eriR = numpy.zeros((nao_pair,nao_pair))
        for LpqR, LpqI in mydf.sr_loop(kptijkl[:2], max_memory, True):
            lib.ddot(LpqR.T, LpqR, 1, eriR, 1)
            LpqR = LpqI = None
        if not compact:
            eriR = ao2mo.restore(1, eriR, nao).reshape(nao**2,-1)
        eriR = mpi.reduce(eriR)
        if rank == 0:
            return eriR

####################
# (kpt) i == j == k == l != 0
#
# (kpt) i == l && j == k && i != j && j != k  =>
# both vbar and ovlp are zero. It corresponds to the exchange integral.
#
# complex integrals, N^4 elements
    elif (abs(kpti-kptl).sum() < 1e-9) and (abs(kptj-kptk).sum() < 1e-9):
        eriR = numpy.zeros((nao*nao,nao*nao))
        eriI = numpy.zeros((nao*nao,nao*nao))
        for LpqR, LpqI in mydf.sr_loop(kptijkl[:2], max_memory, False):
            zdotNC(LpqR.T, LpqI.T, LpqR, LpqI, 1, eriR, eriI, 1)
            LpqR = LpqI = None
# transpose(0,1,3,2) because
# j == k && i == l  =>
# (L|ij).transpose(0,2,1).conj() = (L^*|ji) = (L^*|kl)  =>  (M|kl)
# rho_rs(-G+k_rs) = conj(transpose(rho_sr(G+k_sr), (0,2,1)))
        eriR = mpi.reduce(eriR)
        eriI = mpi.reduce(eriI)
        if rank == 0:
            return (eriR.reshape((nao,)*4).transpose(0,1,3,2) +
                    eriI.reshape((nao,)*4).transpose(0,1,3,2)*1j).reshape(nao**2,-1)

####################
# aosym = s1, complex integrals
#
# kpti == kptj  =>  kptl == kptk
# If kpti == kptj, (kptl-kptk)*a has to be multiples of 2pi because of the wave
# vector symmetry.  k is a fraction of reciprocal basis, 0 < k/b < 1, by definition.
# So  kptl/b - kptk/b  must be -1 < k/b < 1.
#
    else:
        eriR = numpy.zeros((nao*nao,nao*nao))
        eriI = numpy.zeros((nao*nao,nao*nao))
        for (LpqR, LpqI), (LrsR, LrsI) in \
                lib.izip(mydf.sr_loop(kptijkl[:2], max_memory, False),
                         mydf.sr_loop(kptijkl[2:], max_memory, False)):
            zdotNN(LpqR.T, LpqI.T, LrsR, LrsI, 1, eriR, eriI, 1)
            LpqR = LpqI = LrsR = LrsI = None
        eriR = mpi.reduce(eriR)
        eriI = mpi.reduce(eriI)
        if rank == 0:
            return eriR + eriI*1j
get_eri = mpi.parallel_call(_get_eri)


@mpi.parallel_call
def general(mydf, mo_coeffs, kpts=None, compact=True):
    eri = _get_eri(mydf, kpts)
    if rank != 0:
        return

    if isinstance(mo_coeffs, numpy.ndarray) and mo_coeffs.ndim == 2:
        mo_coeffs = (mo_coeffs,) * 4

####################
# gamma point, the integral is real and with s4 symmetry
    if eri.dtype == numpy.float64:
        return ao2mo.general(eri, mo_coeffs, compact=compact)
    else:
        mokl, klslice = ao2mo.incore._conc_mos(mo_coeffs[2], mo_coeffs[3],
                                               False)[2:]
        if mokl.dtype == numpy.float64:
            mokl = mokl + 0j
        nao = mo_coeffs[0].shape[0]
        nmoi = mo_coeffs[0].shape[1]
        nmoj = mo_coeffs[1].shape[1]
        nmok = mo_coeffs[2].shape[1]
        nmol = mo_coeffs[3].shape[1]
        moi = numpy.asarray(mo_coeffs[0], order='F')
        moj = numpy.asarray(mo_coeffs[1], order='F')
        tao = [0]
        ao_loc = None
        pqkl = _ao2mo.r_e2(eri.reshape(-1,nao**2), mokl, klslice, tao, ao_loc, aosym='s1')
        pqkl = pqkl.reshape(nao,nao,nmok*nmol)
        pjkl = numpy.empty((nao,nmoj,nmok*nmol), dtype=numpy.complex128)
        for i in range(nao):
            lib.dot(moj.T, pqkl[i], 1, pjkl[i], 0)
        pqkl = None
        eri_mo = lib.dot(moi.T.conj(), pjkl.reshape(nao,-1))
        return eri_mo.reshape(nmoi*nmoj,-1)


if __name__ == '__main__':
    import pyscf.pbc.gto as pgto
    from pyscf.pbc.df import mdf

    L = 5.
    n = 5
    cell = pgto.Cell()
    cell.a = numpy.diag([L,L,L])
    cell.gs = numpy.array([n,n,n])

    cell.atom = '''He    3.    2.       3.
                   He    1.    1.       1.'''
    #cell.basis = {'He': [[0, (1.0, 1.0)]]}
    #cell.basis = '631g'
    #cell.basis = {'He': [[0, (2.4, 1)], [1, (1.1, 1)]]}
    cell.basis = 'ccpvdz'
    cell.verbose = 0
    cell.build(0,0)

    nao = cell.nao_nr()
    numpy.random.seed(1)
    kpts = numpy.random.random((4,3))
    kpts[3] = -numpy.einsum('ij->j', kpts[:3])
    with_df = mdf.MDF(cell)
    with_df.kpts = kpts
    mo =(numpy.random.random((nao,nao)) +
         numpy.random.random((nao,nao))*1j)
    eri = with_df.get_eri(kpts).reshape((nao,)*4)
    eri0 = numpy.einsum('pjkl,pi->ijkl', eri , mo.conj())
    eri0 = numpy.einsum('ipkl,pj->ijkl', eri0, mo       )
    eri0 = numpy.einsum('ijpl,pk->ijkl', eri0, mo.conj())
    eri0 = numpy.einsum('ijkp,pl->ijkl', eri0, mo       )
    eri1 = with_df.ao2mo(mo, kpts)
    print abs(eri1-eri0).sum()
