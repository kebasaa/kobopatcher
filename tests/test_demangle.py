"""Tests for kobopatcher.patchlib._demangle, the Python port of
github.com/ianlancetaylor/demangle v0.0.0-20250628045327-2d64ad6b7ec5.

kobopatch only calls ``demangle.ToString(name)`` with default options,
so only vectors for that configuration are ported:

* TestDemangler (demangle_test.go): the ``want`` column (default
  options) of all 47 entries is ported below.  The wantNoParams,
  wantNoTemplateParams, wantNoEnclosingParams and wantMinimal columns
  and the Filter checks are skipped (non-default options / other API).
* TestFailure (demangle_test.go): all 17 entries ported (error text and
  offset).  The Filter check is skipped (other API).
* TestASTToString (ast_test.go): the ASTToString half is ported; the
  GoString formatting half is skipped (not ported).
* TestExpected (expected_test.go, testdata/demangle-expected): the 321
  default-option expectations are in
  a fixture outside the repository (see test_expected_files).  Skipped exactly as
  upstream skips them (non _Z/_GLOBAL_ inputs, java/gnat formats,
  --ret-postfix, --ret-drop, --is-v3-ctor/dtor, and the 13 listed
  exceptions), plus the 117 --no-params expectations (NoParams option)
  and the MaxLength(6) checks (MaxLength option).
* TestRustExpected (rust_expected_test.go, testdata/rust-demangle-
  expected): the 74 --format=rust/auto entries are in the same fixture;
  the one --format=gnu-v3 entry is skipped as upstream does, and the
  MaxLength(6) checks are skipped (MaxLength option).
* Skipped entirely because they need non-default options: TestCases
  (testdata/DemangleTestCases.inc, LLVMStyle), TestRustCheck
  (testdata/rust.test, LLVMStyle), TestRustNoTemplaraParams
  (NoTemplateParams), TestOptionsSlice (NoParams etc.), TestMaxLength
  (MaxLength).  Their inputs are still covered: all upstream test inputs
  are part of the differential corpus below, with the Go ToString results.

testdata/demangle_corpus.txt.gz holds the results of Go's
demangle.ToString (default options) for every dynamic symbol name of
the Kobo firmware ELF files (fw-4.7/4.30/4.34/4.38 /usr/local/Kobo and
/usr/local/Trolltech, plus tests/testdata/libnickel.so.1.0.0 and
tests/testdata/nickel), all upstream test inputs, deep names, non-ASCII
names and fuzzed names covering every failure mode seen (including
inputs on which Go panics).  It was produced with
tools/parity/demangle_diff.py.
"""

from __future__ import annotations

import gzip
import sys
import os
import random
import struct
import time
from pathlib import Path

import pytest

import kobopatcher.patchlib._demangle as demangle_pkg
from kobopatcher.patchlib._demangle import DemangleError, to_string
from kobopatcher.patchlib._demangle.ast import BuiltinType, Name, Qualified, Typed, ast_to_string
from kobopatcher.patchlib._demangle.errors import NotMangledNameError

TESTDATA = Path(__file__).parent / "testdata"

# (input, want) from TestDemangler: the `want` column is ToString(input).
DEMANGLER_TESTS = [
    (
        '_ZNSaIcEC1ERKS_',
        'std::allocator<char>::allocator(std::allocator<char> const&)',
    ),
    (
        '_ZN9__gnu_cxx13stdio_filebufIcSt11char_traitsIcEEC1EP8_IO_FILESt13_Ios_Openmodem',
        '__gnu_cxx::stdio_filebuf<char, std::char_traits<char> >::stdio_filebuf(_IO_FILE*, std::_Ios_Openmode, unsigned long)',
    ),
    (
        '_ZN1n1CcvNS_1DIT_EEI1EEEv',
        'n::C::operator n::D<E><E>()',
    ),
    (
        '_Z1CIvPN1D1E1FIdJEEEdEPN1GILb0ET_T0_T1_E1HEPFS6_S7_S8_EN1H1I1JIS7_E1KENSG_IS8_E1KE',
        'G<false, void, D::E::F<double>*, double>::H* C<void, D::E::F<double>*, double>(void (*)(D::E::F<double>*, double), H::I::J<D::E::F<double>*>::K, H::I::J<double>::K)',
    ),
    (
        '_ZZNK1CI1DIcSt1EIcESaIcEEJEE1FEvE1F',
        'C<D<char, std::E<char>, std::allocator<char> > >::F() const::F',
    ),
    (
        '_ZN1CI1DSt1EIK1FN1G1HEEE1I1JIJRKS6_EEEvDpOT_',
        'void C<D, std::E<F const, G::H> >::I::J<std::E<F const, G::H> const&>(std::E<F const, G::H> const&)',
    ),
    (
        '_ZN1C1D1E1FIJEEEvi1GDpT_',
        'void C::D::E::F<>(int, G)',
    ),
    (
        '_ZN1CILj50ELb1EE1DEv',
        'C<50u, true>::D()',
    ),
    (
        '_ZN1CUt_C2Ev',
        'C::{unnamed type#1}::{unnamed type#1}()',
    ),
    (
        '_ZN1C12_GLOBAL__N_11DINS_1EEEEN1F1GIDTadcldtcvT__E1HEEEERKS5_NS_1I1JE',
        'F::G<decltype (&((((C::E)()).H)()))> C::(anonymous namespace)::D<C::E>(C::E const&, C::I::J)',
    ),
    (
        '_ZN1CI1DE1EIJiRiRPKcRA1_S4_S8_bS6_S3_RjRPKN1F1GERPKN1H1IEEEEvDpOT_',
        'void C<D>::E<int, int&, char const*&, char const (&) [1], char const (&) [1], bool, char const*&, int&, unsigned int&, F::G const*&, H::I const*&>(int&&, int&, char const*&, char const (&) [1], char const (&) [1], bool&&, char const*&, int&, unsigned int&, F::G const*&, H::I const*&)',
    ),
    (
        '_ZN1C12_GLOBAL__N_11DIFbPKNS_1EEEEEvPNS_1FERKT_',
        'void C::(anonymous namespace)::D<bool (C::E const*)>(C::F*, bool (&)(C::E const*) const)',
    ),
    (
        '_ZN1C1D1EIJRFviSt1FIFvRKN1G1H1IEEERKSt6vectorINS_1JESaISB_EEERiS9_EvEENS0_1K1LIJDpNSt1MIT_E1NEEEEDpOSM_',
        'C::D::K::L<std::M<void (&)(int, std::F<void (G::H::I const&)>, std::vector<C::J, std::allocator<C::J> > const&)>::N, std::M<int&>::N, std::M<std::F<void (G::H::I const&)> >::N> C::D::E<void (&)(int, std::F<void (G::H::I const&)>, std::vector<C::J, std::allocator<C::J> > const&), int&, std::F<void (G::H::I const&)>, void>(void (&)(int, std::F<void (G::H::I const&)>, std::vector<C::J, std::allocator<C::J> > const&), int&, std::F<void (G::H::I const&)>&&)',
    ),
    (
        '_ZN1C1D1E1FcvNS_1GIT_EEI1HEEv',
        'C::D::E::F::operator C::G<H><H>()',
    ),
    (
        '_ZN9__gnu_cxx17__normal_iteratorIPK1EIN1F1G1HEESt6vectorIS5_SaIS5_EEEC2IPS5_EERKNS0_IT_NS_11__enable_ifIXsr3std10__are_sameISE_SD_EE7__valueESA_E1IEEE',
        '__gnu_cxx::__normal_iterator<E<F::G::H> const*, std::vector<E<F::G::H>, std::allocator<E<F::G::H> > > >::__normal_iterator<E<F::G::H>*>(__gnu_cxx::__normal_iterator<E<F::G::H>*, __gnu_cxx::__enable_if<std::__are_same<E<F::G::H>*, E<F::G::H>*>::__value, std::vector<E<F::G::H>, std::allocator<E<F::G::H> > > >::I> const&)',
    ),
    (
        '_ZNKSt1CIM1DKFjvEEclIJEvEEjPKS0_DpOT_',
        'unsigned int std::C<unsigned int (D::*)() const>::operator()<void>(D const*) const',
    ),
    (
        '_ZNSt10_HashtableI12basic_stringIcSt11char_traitsIcESaIcEESt4pairIKS4_N1C1D1EEESaISA_ENSt8__detail10_Select1stESt8equal_toIS4_ESt4hashIS4_ENSC_18_Mod_range_hashingENSC_20_Default_ranged_hashENSC_20_Prime_rehash_policyENSC_17_Hashtable_traitsILb1ELb0ELb1EEEE9_M_assignIZNSN_C1ERKSN_EUlPKNSC_10_Hash_nodeISA_Lb1EEEE_EEvSQ_RKT_',
        'void std::_Hashtable<basic_string<char, std::char_traits<char>, std::allocator<char> >, std::pair<basic_string<char, std::char_traits<char>, std::allocator<char> > const, C::D::E>, std::allocator<std::pair<basic_string<char, std::char_traits<char>, std::allocator<char> > const, C::D::E> >, std::__detail::_Select1st, std::equal_to<basic_string<char, std::char_traits<char>, std::allocator<char> > >, std::hash<basic_string<char, std::char_traits<char>, std::allocator<char> > >, std::__detail::_Mod_range_hashing, std::__detail::_Default_ranged_hash, std::__detail::_Prime_rehash_policy, std::__detail::_Hashtable_traits<true, false, true> >::_M_assign<std::_Hashtable<basic_string<char, std::char_traits<char>, std::allocator<char> >, std::pair<basic_string<char, std::char_traits<char>, std::allocator<char> > const, C::D::E>, std::allocator<std::pair<basic_string<char, std::char_traits<char>, std::allocator<char> > const, C::D::E> >, std::__detail::_Select1st, std::equal_to<basic_string<char, std::char_traits<char>, std::allocator<char> > >, std::hash<basic_string<char, std::char_traits<char>, std::allocator<char> > >, std::__detail::_Mod_range_hashing, std::__detail::_Default_ranged_hash, std::__detail::_Prime_rehash_policy, std::__detail::_Hashtable_traits<true, false, true> >::_Hashtable(std::_Hashtable<basic_string<char, std::char_traits<char>, std::allocator<char> >, std::pair<basic_string<char, std::char_traits<char>, std::allocator<char> > const, C::D::E>, std::allocator<std::pair<basic_string<char, std::char_traits<char>, std::allocator<char> > const, C::D::E> >, std::__detail::_Select1st, std::equal_to<basic_string<char, std::char_traits<char>, std::allocator<char> > >, std::hash<basic_string<char, std::char_traits<char>, std::allocator<char> > >, std::__detail::_Mod_range_hashing, std::__detail::_Default_ranged_hash, std::__detail::_Prime_rehash_policy, std::__detail::_Hashtable_traits<true, false, true> > const&)::{lambda(std::__detail::_Hash_node<std::pair<basic_string<char, std::char_traits<char>, std::allocator<char> > const, C::D::E>, true> const*)#1}>(std::_Hashtable<basic_string<char, std::char_traits<char>, std::allocator<char> >, std::pair<basic_string<char, std::char_traits<char>, std::allocator<char> > const, C::D::E>, std::allocator<std::pair<basic_string<char, std::char_traits<char>, std::allocator<char> > const, C::D::E> >, std::__detail::_Select1st, std::equal_to<basic_string<char, std::char_traits<char>, std::allocator<char> > >, std::hash<basic_string<char, std::char_traits<char>, std::allocator<char> > >, std::__detail::_Mod_range_hashing, std::__detail::_Default_ranged_hash, std::__detail::_Prime_rehash_policy, std::__detail::_Hashtable_traits<true, false, true> > const&, std::_Hashtable<basic_string<char, std::char_traits<char>, std::allocator<char> >, std::pair<basic_string<char, std::char_traits<char>, std::allocator<char> > const, C::D::E>, std::allocator<std::pair<basic_string<char, std::char_traits<char>, std::allocator<char> > const, C::D::E> >, std::__detail::_Select1st, std::equal_to<basic_string<char, std::char_traits<char>, std::allocator<char> > >, std::hash<basic_string<char, std::char_traits<char>, std::allocator<char> > >, std::__detail::_Mod_range_hashing, std::__detail::_Default_ranged_hash, std::__detail::_Prime_rehash_policy, std::__detail::_Hashtable_traits<true, false, true> >::_Hashtable(std::_Hashtable<basic_string<char, std::char_traits<char>, std::allocator<char> >, std::pair<basic_string<char, std::char_traits<char>, std::allocator<char> > const, C::D::E>, std::allocator<std::pair<basic_string<char, std::char_traits<char>, std::allocator<char> > const, C::D::E> >, std::__detail::_Select1st, std::equal_to<basic_string<char, std::char_traits<char>, std::allocator<char> > >, std::hash<basic_string<char, std::char_traits<char>, std::allocator<char> > >, std::__detail::_Mod_range_hashing, std::__detail::_Default_ranged_hash, std::__detail::_Prime_rehash_policy, std::__detail::_Hashtable_traits<true, false, true> > const&)::{lambda(std::__detail::_Hash_node<std::pair<basic_string<char, std::char_traits<char>, std::allocator<char> > const, C::D::E>, true> const*)#1} const&)',
    ),
    (
        '_ZSt3maxIVdERKT_S3_S3_',
        'double const volatile& std::max<double volatile>(double const volatile&, double const volatile&)',
    ),
    (
        '_ZZN1C1D1E1F1G1HEvENUlvE_C2EOS4_',
        'C::D::E::F::G::H()::{lambda()#1}::{lambda()#1}({lambda()#1}&&)',
    ),
    (
        '_ZThn8_NK1C1D1EEv',
        'non-virtual thunk to C::D::E() const',
    ),
    (
        '_ZTv0_n96_NK1C1D1E1FEv',
        'virtual thunk to C::D::E::F() const',
    ),
    (
        '_ZTCSt9strstream16_So',
        'construction vtable for std::ostream-in-std::strstream',
    ),
    (
        '_ZGVZZN1C1D1EEvENK3$_0clEvE1F',
        'guard variable for C::D::E()::$_0::operator()() const::F',
    ),
    (
        '_Z1fICiEvT_',
        'void f<int _Complex>(int _Complex)',
    ),
    (
        '_GLOBAL__D__Z2fnv',
        'global destructors keyed to fn()',
    ),
    (
        '_Z1fIXadL_Z1hvEEEvv',
        'void f<&h>()',
    ),
    (
        '_Z1CIP1DEiRK1EPT_N1F1GIS5_Xaasr1HIS5_E1IntsrSA_1JEE1KE',
        'int C<D*>(E const&, D**, F::G<D*, H<D*>::I&&(!H::J)>::K)',
    ),
    (
        '_ZNO1A1B1C1DIZN1E1F1GINS3_1HE1IEEvMNS3_1JEFvP1LPKT_PT0_P1KESD_SA_SF_SH_EUlvE_Lb0EEcvPSB_ISG_vvEEv',
        'A::B::C::D<E::F::G<E::H, I>(void (E::J::*)(L*, E::H const*, I*, K*), E::H const*, L*, I*, K*)::{lambda()#1}, false>::operator K*<K, void, void>() &&',
    ),
    (
        '_ZNSt1AIFSt1BImjEjEZN1C1DI1EEENSt1FIXeqsr1G1H1IIDTadsrT_onclEEE1JLi2EEvE1KEPKcSC_OS7_EUljE_E1KERKSt1Lj',
        'std::A<std::B<unsigned long, unsigned int> (unsigned int), C::D<E>(char const*, char const, I&&)::{lambda(unsigned int)#1}>::K(std::L const&, unsigned int)',
    ),
    (
        '_ZNSt1A1BIiNS_1CIiEEE1DIPiEENS_1EIXaasr1FIT_EE1Gsr1HIiNS_1IIS7_E1JEEE1KEvE1LES7_S7_',
        'std::A::E<F<int*>::G&&H<int, std::A::I<F>::J>::K, void>::L std::A::B<int, std::A::C<int> >::D<int*>(F, F)',
    ),
    (
        '_ZNO1A1B1C1DIJOZZN1E1F1GINS4_1HINS4_1IINS4_1JEEEEEJNS4_1KEEEEN1L1MINS4_1OINT_1PEEEEERKSt6vectorIN1Q1RESaISL_EERKN3gtl1S1TIN1U1VEEERKNS4_1W1XERKNS4_1YERKNSQ_1ZINS4_1aEEEPSt13unordered_mapISL_NSK_9UniquePtrINS4_1bINS0_1cIJS9_NS7_INST_1dEEEEEENS4_1fEEEEENSC_1g1hIvEESt8equal_toISL_ESaISt4pairIKSL_S1J_EEEDpRKT0_ENKUlSL_mmS1G_E_clESL_mmS1G_EUlS9_E_OZZNS5_ISA_JSB_EEESI_SP_SX_S11_S14_S19_S1U_S1Y_ENKS1Z_clESL_mmS1G_EUlS1F_E0_EEclIJRS9_EEEDTclcl1iIXsrNS1_1jISt5tupleIJNS1_1kIS21_EENS29_IS23_EEEEJDpT_EEE1lEEcl1mIS2C_EEEspcl1mIS2D_EEEEDpOS2D_',
        'decltype (((i<A::B::C::j<std::tuple<A::B::C::k<E::F::G<E::F::H<E::F::I<E::F::J> >, E::F::K>(std::vector<Q::R, std::allocator<Q::R> > const&, gtl::S::T<U::V> const&, E::F::W::X const&, E::F::Y const&, gtl::Z<E::F::a> const&, std::unordered_map<Q::R, Q::UniquePtr<E::F::b<A::B::c<E::F::I<E::F::J>, E::F::I<U::d> >, E::F::f> >, L::g::h<void>, std::equal_to<Q::R>, std::allocator<std::pair<Q::R const, Q::UniquePtr<E::F::b<A::B::c<E::F::I<E::F::J>, E::F::I<U::d> >, E::F::f> > > > >*, E::F::K const&)::{lambda(Q::R, unsigned long, unsigned long, A::B::c<E::F::I<E::F::J>, E::F::I<U::d> >)#1}::operator()(Q::R, unsigned long, unsigned long, A::B::c<E::F::I<E::F::J>, E::F::I<U::d> >) const::{lambda(E::F::I<E::F::J>)#1}&&>, A::B::C::k<E::F::G<E::F::H<E::F::I<E::F::J> >, E::F::K>(std::vector<Q::R, std::allocator<Q::R> > const&, gtl::S::T<U::V> const&, E::F::W::X const&, E::F::Y const&, gtl::Z<E::F::a> const&, std::unordered_map<Q::R, Q::UniquePtr<E::F::b<A::B::c<E::F::I<E::F::J>, E::F::I<U::d> >, E::F::f> >, L::g::h<void>, std::equal_to<Q::R>, std::allocator<std::pair<Q::R const, Q::UniquePtr<E::F::b<A::B::c<E::F::I<E::F::J>, E::F::I<U::d> >, E::F::f> > > > >*, E::F::K const&)::{lambda(Q::R, unsigned long, unsigned long, A::B::c<E::F::I<E::F::J>, E::F::I<U::d> >)#1}::operator()(Q::R, unsigned long, unsigned long, A::B::c<E::F::I<E::F::J>, E::F::I<U::d> >) const::{lambda(E::F::I<U::d>)#2}&&> >, E::F::I<E::F::J>&>::l>)((m<std::tuple<A::B::C::k<E::F::G<E::F::H<E::F::I<E::F::J> >, E::F::K>(std::vector<Q::R, std::allocator<Q::R> > const&, gtl::S::T<U::V> const&, E::F::W::X const&, E::F::Y const&, gtl::Z<E::F::a> const&, std::unordered_map<Q::R, Q::UniquePtr<E::F::b<A::B::c<E::F::I<E::F::J>, E::F::I<U::d> >, E::F::f> >, L::g::h<void>, std::equal_to<Q::R>, std::allocator<std::pair<Q::R const, Q::UniquePtr<E::F::b<A::B::c<E::F::I<E::F::J>, E::F::I<U::d> >, E::F::f> > > > >*, E::F::K const&)::{lambda(Q::R, unsigned long, unsigned long, A::B::c<E::F::I<E::F::J>, E::F::I<U::d> >)#1}::operator()(Q::R, unsigned long, unsigned long, A::B::c<E::F::I<E::F::J>, E::F::I<U::d> >) const::{lambda(E::F::I<E::F::J>)#1}&&>, A::B::C::k<E::F::G<E::F::H<E::F::I<E::F::J> >, E::F::K>(std::vector<Q::R, std::allocator<Q::R> > const&, gtl::S::T<U::V> const&, E::F::W::X const&, E::F::Y const&, gtl::Z<E::F::a> const&, std::unordered_map<Q::R, Q::UniquePtr<E::F::b<A::B::c<E::F::I<E::F::J>, E::F::I<U::d> >, E::F::f> >, L::g::h<void>, std::equal_to<Q::R>, std::allocator<std::pair<Q::R const, Q::UniquePtr<E::F::b<A::B::c<E::F::I<E::F::J>, E::F::I<U::d> >, E::F::f> > > > >*, E::F::K const&)::{lambda(Q::R, unsigned long, unsigned long, A::B::c<E::F::I<E::F::J>, E::F::I<U::d> >)#1}::operator()(Q::R, unsigned long, unsigned long, A::B::c<E::F::I<E::F::J>, E::F::I<U::d> >) const::{lambda(E::F::I<U::d>)#2}&&> > >)()))((m<E::F::I<E::F::J>&>)())) A::B::C::D<E::F::G<E::F::H<E::F::I<E::F::J> >, E::F::K>(std::vector<Q::R, std::allocator<Q::R> > const&, gtl::S::T<U::V> const&, E::F::W::X const&, E::F::Y const&, gtl::Z<E::F::a> const&, std::unordered_map<Q::R, Q::UniquePtr<E::F::b<A::B::c<E::F::I<E::F::J>, E::F::I<U::d> >, E::F::f> >, L::g::h<void>, std::equal_to<Q::R>, std::allocator<std::pair<Q::R const, Q::UniquePtr<E::F::b<A::B::c<E::F::I<E::F::J>, E::F::I<U::d> >, E::F::f> > > > >*, E::F::K const&)::{lambda(Q::R, unsigned long, unsigned long, A::B::c<E::F::I<E::F::J>, E::F::I<U::d> >)#1}::operator()(Q::R, unsigned long, unsigned long, A::B::c<E::F::I<E::F::J>, E::F::I<U::d> >) const::{lambda(E::F::I<E::F::J>)#1}&&, E::F::G<E::F::H<E::F::I<E::F::J> >, E::F::K>(std::vector<Q::R, std::allocator<Q::R> > const&, gtl::S::T<U::V> const&, E::F::W::X const&, E::F::Y const&, gtl::Z<E::F::a> const&, std::unordered_map<Q::R, Q::UniquePtr<E::F::b<A::B::c<E::F::I<E::F::J>, E::F::I<U::d> >, E::F::f> >, L::g::h<void>, std::equal_to<Q::R>, std::allocator<std::pair<Q::R const, Q::UniquePtr<E::F::b<A::B::c<E::F::I<E::F::J>, E::F::I<U::d> >, E::F::f> > > > >*, E::F::K const&)::{lambda(Q::R, unsigned long, unsigned long, A::B::c<E::F::I<E::F::J>, E::F::I<U::d> >)#1}::operator()(Q::R, unsigned long, unsigned long, A::B::c<E::F::I<E::F::J>, E::F::I<U::d> >) const::{lambda(E::F::I<U::d>)#2}&&>::operator()<E::F::I<E::F::J>&>(E::F::I<E::F::J>&) &&',
    ),
    (
        '_ZcvAna_eE_e',
        'operator long double [new long double]',
    ),
    (
        '_ZZ1irFeeEES_S_',
        'i(() restrict)::long double (long double)(() restrict) restrict',
    ),
    (
        '_Z1_VFaeEZS_S_ES_',
        '_((() volatile) volatile, signed char (long double)(() volatile) volatile::(() volatile) volatile)',
    ),
    (
        '_ZdsrFliEZS_GS_EcvS_',
        'operator.*(( ( _Imaginary)( _Imaginary) restrict) restrict, long (int)( ( _Imaginary)( _Imaginary) restrict) restrict::operator ( ( _Imaginary)( _Imaginary) restrict) restrict)',
    ),
    (
        '_ZZN1A1B1CIfEEvPNS_1DERKNS_1EEiS6_T_S7_S7_RKNSt3__u1FIFS7_iiEEEbbPiENKUlZNS1_IfEEvS3_S6_iS6_S7_S7_S7_SD_bbSE_E1GSF_E_clESF_SF_',
        'A::B::C<float>(A::D*, A::E const&, int, A::E const&, float, float, float, std::__u::F<float (int, int)> const&, bool, bool, int*)::{lambda(A::B::C<float>(A::D*, A::E const&, int, A::E const&, float, float, float, std::__u::F<float (int, int)> const&, bool, bool, int*)::G, A::B::C<float>(A::D*, A::E const&, int, A::E const&, float, float, float, std::__u::F<float (int, int)> const&, bool, bool, int*)::G)#1}::operator()(A::B::C<float>(A::D*, A::E const&, int, A::E const&, float, float, float, std::__u::F<float (int, int)> const&, bool, bool, int*)::G, A::B::C<float>(A::D*, A::E const&, int, A::E const&, float, float, float, std::__u::F<float (int, int)> const&, bool, bool, int*)::G) const',
    ),
    (
        '_ZN1A1B1CIJLNS_1DE131067ELS2_4EEEC2EUa9enable_ifIXclL_ZNS0_1EIJLS2_131067ELS2_4EEEEbNSt1F1GIcNS5_1HIcEEEEEfL0p_EEEPKc',
        'A::B::C<(A::D)131067, (A::D)4>::C(char const*) [enable_if:bool A::B::E<(A::D)131067, (A::D)4>(std::F::G<char, std::F::H<char> >)({parm#1})]',
    ),
    (
        '_ZNK1A1B1C1DINS0_1EIKZNK1F1G1HIJNS4_1IINSt1J1KIcNS8_1LIcEENS8_1MIcEEEEEENS4_1NIixEEEE1OEvEUlRT_E_EERKNS8_1PIJNS4_1QISF_EENSP_ISH_EEEEEEclILm0EEEDTcldtclL_ZNS8_1RIRKSN_EEDTclsr3std1SE1TISJ_ELi0EEEvEEonclIXT_EEcl1UIXT_EEclL_ZNSX_ISU_EES10_vEEEEEv',
        'decltype (((decltype (std::S::T<A::B::E<F::G::H<F::I<std::J::K<char, std::J::L<char>, std::J::M<char> > >, F::N<int, long long> >::O() const::{lambda(auto:1&)#1} const> const&>(0)) std::J::R<A::B::E<F::G::H<F::I<std::J::K<char, std::J::L<char>, std::J::M<char> > >, F::N<int, long long> >::O() const::{lambda(auto:1&)#1} const> const&>()()).(operator()<0ul>))((U<0ul>)(decltype (std::S::T<std::J::P<F::Q<F::I<std::J::K<char, std::J::L<char>, std::J::M<char> > > >, F::Q<F::N<int, long long> > > const&>(0)) std::J::R<std::J::P<F::Q<F::I<std::J::K<char, std::J::L<char>, std::J::M<char> > > >, F::Q<F::N<int, long long> > > const&>()()))) A::B::C::D<A::B::E<F::G::H<F::I<std::J::K<char, std::J::L<char>, std::J::M<char> > >, F::N<int, long long> >::O() const::{lambda(auto:1&)#1} const>, std::J::P<F::Q<F::I<std::J::K<char, std::J::L<char>, std::J::M<char> > > >, F::Q<F::N<int, long long> > > const&>::operator()<0ul>() const',
    ),
    (
        '_ZN1A1B1CIKZN1D1E1FIJNS2_1GINSt1H1IIcNS6_1JIcEENS6_1KIcEEEEiEENS5_IixEEEE1LEvEUlRT_E_EC2EPSJ_',
        'A::B::C<D::E::F<D::G<std::H::I<char, std::H::J<char>, std::H::K<char> >, int>, D::G<int, long long> >::L()::{lambda(auto:1&)#1} const>::C(D::E::F<D::G<std::H::I<char, std::H::J<char>, std::H::K<char> >, int>, D::G<int, long long> >::L()::{lambda(auto:1&)#1} const*)',
    ),
    (
        '_ZN1A1B1CILb0EvOZN1D1E1FINS3_1GENS3_1HENS3_1IEE1JEvEUlvE0_JEEET0_PNS0_1KEDpDTcp1KIT2_EcvNSt1L1MIbXsr1NE1OISE_EEEE_EEE',
        'void A::B::C<false, void, D::E::F<D::E::G, D::E::H, D::E::I>::J()::{lambda()#2}&&>(A::B::K*)',
    ),
    (
        '_ZN1A1BIJNS_1C1DIJNS_1EENS_1FEEEEENSt1G1HIcNS6_1IIcEENS6_1JIcEEEEEEDTcl1KINS1_1KIsr1LIT0_EE1NJNS1_1MIJDpT_EE1NEEE1N1NEEcldtfp_1OEfp0_EERKNS_1PISE_EEPKc',
        'decltype ((K<A::C::K<short, L<std::G::H<char, std::G::I<char>, std::G::J<char> > > restrict>::N<A::C::M<A::C::D<A::E, A::F> >::N>::N::N>)(({parm#1}.O)(), {parm#2})) A::B<A::C::D<A::E, A::F>, std::G::H<char, std::G::I<char>, std::G::J<char> > >(A::P<L> const&, char const*)',
    ),
    (
        '_ZNSt1A1B1CIZN1D1E1F1GIZNS3_1H1IEixxE1JEENS_1KIFDTclclsr3stdE7declvalIT_EEEEvEEEPN1L1MENS_1OIcNS_1PIcEEEES9_EUlvE_FN1Q1RINS_1SIJNSD_1T1U1VENS6_1WEEEEEEvEEclEv',
        'std::A::B::C<D::E::F::G<D::E::H::I(int, long long, long long)::J>(L::M*, std::A::O<char, std::A::P<char> >, D::E::H::I(int, long long, long long)::J)::{lambda()#1}, Q::R<std::A::S<L::T::U::V, D::E::H::W> > ()>::operator()()',
    ),
    (
        '_ZNSt1A1B1CIFN1D1EIN1F1G1HEEERKNS5_1IEEE1JINS0_1KIZN1L1MINS2_1NIS8_S6_EENS5_12_GLOBAL__N_11OES8_S6_EEN1P1QINS_1RINSH_IT1_T2_EENS_1SISQ_EEEEEENS_1TIKT_EENSF_1UET0_EUlSA_E_SB_EEEES7_PKNS0_1VESA_',
        'D::E<F::G::H> std::A::B::C<D::E<F::G::H> (F::G::I const&)>::J<std::A::B::K<L::M<D::N<F::G::I, F::G::H>, F::G::(anonymous namespace)::O, F::G::I, F::G::H>(std::A::T<D::N<F::G::I, F::G::H> const>, L::U, F::G::(anonymous namespace)::O)::{lambda(F::G::I const&)#1}, D::E<F::G::H> (F::G::I const&)> >(std::A::B::V const*, F::G::I const&)',
    ),
    (
        '_ZGVZNK1A1B1CIZNKS0_1DIXadL_ZNS_1EIvE1FEvEES4_EcvNS_1GIFT_DpT0_EEEIvJEvEEvEUlPS4_E_S4_EcvSB_IvJEEEvE1H',
        'guard variable for A::B::C<A::B::D<&A::E<void>::F, A::E<void> >::operator A::G<void (()...)><void, void>() const::{lambda(A::E<void>*)#1}, A::E<void> >::operator A::G<void (()...)><void>() const::H',
    ),
    (
        '_ZNKSt2Cr1AIN1B1CENS_1DIS2_EEEcvbB7v170000Ev',
        'std::Cr::A<B::C, std::Cr::D<B::C> >::operator bool[abi:v170000]() const',
    ),
    (
        '_ZN1A1B1CIZN1D1E1FINS2_2KVINSt3__u1GIcNS6_1HIcEENS6_1IIcEEEESC_EEEENS_1JERKN1K1L1M1NENS_1OIFvvEEENSL_IFvRKT_EEEPNS6_1PIXsr1Q1R1SISO_EE1TENSH_1TISO_EENSH_1UISO_EEE1VEEUlRKNSH_1WES13_E_vJS13_S13_EEET0_NS0_1XEDpNS0_1YIT1_E1ZE',
        'void A::B::C<D::E::F<D::KV<std::__u::G<char, std::__u::H<char>, std::__u::I<char> >, std::__u::G<char, std::__u::H<char>, std::__u::I<char> > > >(K::L::M::N const&, A::O<void ()>, A::O<void (D::KV<std::__u::G<char, std::__u::H<char>, std::__u::I<char> >, std::__u::G<char, std::__u::H<char>, std::__u::I<char> > > const&)>, std::__u::P<Q::R::S<D::KV<std::__u::G<char, std::__u::H<char>, std::__u::I<char> >, std::__u::G<char, std::__u::H<char>, std::__u::I<char> > > >::T, K::L::M::T<D::KV<std::__u::G<char, std::__u::H<char>, std::__u::I<char> >, std::__u::G<char, std::__u::H<char>, std::__u::I<char> > > >, K::L::M::U<D::KV<std::__u::G<char, std::__u::H<char>, std::__u::I<char> >, std::__u::G<char, std::__u::H<char>, std::__u::I<char> > > > >::V*)::{lambda(K::L::M::W const&, K::L::M::W const)#1}, void, K::L::M::W const, K::L::M::W const>(A::B::X, A::B::Y<K::L::M::W const>::Z, A::B::Y<K::L::M::W const>::Z)',
    ),
    (
        '_ZZNK1P1A1B1CIZNS0_1DIJETpTnRiJENS0_1E1F1GEvEENS_1HIFvDpT_EEEN1J1KINS0_1LIT1_EEEEEUlPNSF_IS7_EEE_SJ_EcvNS8_IFT_DpT0_EEEIvJEQsr1ME1NITL0__SN_PT0_DpTL0_0_EEEvENKUllE_clEl',
        'P::A::B::C<P::A::D<, P::A::E::F::G, void>(J::K<P::A::L<P::A::E::F::G> >)::{lambda(P::A::L<P::A::E::F::G>*)#1}, P::A::L<P::A::E::F::G> >::operator P::H<void (()...)><void>() const requires M::N<TL0_, T0, T0*, TL0_0...>::{lambda(long)#1}::operator()(long) const',
    ),
]

# (input, error substring, offset) from TestFailure.
FAILURE_TESTS = [
    ('_Z1FE', 'unparsed characters at end of mangled name', 4),
    ('_Z1FQ', 'expected at least one type', 4),
    ('_ZZSaIL0D', 'expected positive number', 8),
    ('_ZNKE', 'expected prefix', 4),
    ('_ZcvT_', 'not in scope of template', 6),
    ('_Z1AIXsZ1_EE', 'missing argument pack', 8),
    ('_Z1gIEDTclspilE', 'expected expression', 15),
    ('_ZNcvZN1ET_IEE', 'after local name', 14),
    ('_Zv00', 'expected positive number', 5),
    ('_ZcvT_B2T0', 'template parameter not in scope', 10),
    ('_ZStcvT_', 'template parameter not in scope', 8),
    ('_Z1aIeEU1RT_ZcvS1_', 'expected E after local name', 18),
    ('_ZNcvT_oRIEE', 'template index out of range', 11),
    ('_ZNcvT_D0IIEE', 'expected prefix', 13),
    ('_ZcvT_IAoncvT__eE', 'template parameter not in scope', 17),
    ('_ZZF', 'expected unqualified name', 4),
    ('_ZNW1a', 'expected unqualified name', 6),
]


@pytest.mark.parametrize("name,want", DEMANGLER_TESTS, ids=[t[0][:60] for t in DEMANGLER_TESTS])
def test_demangler(name, want):
    assert to_string(name) == want


@pytest.mark.parametrize("name,error,off", FAILURE_TESTS, ids=[t[0] for t in FAILURE_TESTS])
def test_failure(name, error, off):
    with pytest.raises(DemangleError) as ei:
        to_string(name)
    s = str(ei.value)
    assert error in s
    i = s.rfind(" at ")
    assert i >= 0, "missing offset in error for %s: %s" % (name, s)
    assert int(s[i + 4 :]) == off


def test_ast_to_string():
    assert ast_to_string(Qualified(Name("s"), Name("C"))) == "s::C"
    assert ast_to_string(Typed(Name("v"), BuiltinType("int"))) == "int v"


def _read_tsv(path: Path):
    for line in gzip.open(path).read().split(b"\n"):
        if line:
            yield [f.decode("utf-8", "surrogateescape") for f in line.split(b"\t", 2)]


def test_expected_files():
    """oneTest/oneRustTest of upstream, for the default-option lines of
    demangle-expected and rust-demangle-expected."""
    # The expectations come from GCC's (GPL) demangle-expected test file, so
    # they are not part of this repository. Generate them with
    # tools/parity/demangle_diff.py --upstream --write-fixture and point
    # KOBOPATCHER_DEMANGLE_EXPECTED at the file.
    path = os.environ.get("KOBOPATCHER_DEMANGLE_EXPECTED") or os.path.expanduser(
        "~/Documents/Github/kobopatcher-corpus/demangle_corpus_upstream.txt.gz"
    )
    if not os.path.isfile(path):
        pytest.skip("demangle-expected fixture not available (set KOBOPATCHER_DEMANGLE_EXPECTED)")
    failures = []
    n = 0
    for where, name, expect in _read_tsv(path):
        n += 1
        try:
            s = to_string(name)
        except NotMangledNameError:
            s = name
        except DemangleError as e:
            if name != expect:
                failures.append((where, name, "error %s" % e, expect))
            continue
        if s != expect:
            failures.append((where, name, s, expect))
    assert n == 321 + 74
    assert not failures, failures[:20]


def test_corpus_matches_go():
    """Every name of the differential corpus gives exactly Go's result."""
    mismatches = []
    n = 0
    for fields in _read_tsv(TESTDATA / "demangle_corpus.txt.gz"):
        n += 1
        name, status = fields[0], fields[1]
        try:
            got = ("OK", to_string(name))
        except DemangleError:
            got = ("ERR", None)
        want = ("OK", fields[2]) if status == "OK" else ("ERR", None)
        if got != want:
            mismatches.append((name, want, got))
    assert n > 150_000
    assert not mismatches, mismatches[:20]


# One input per kind of Go panic found by fuzzing (Go's ToString panics on
# these instead of returning an error; kobopatch would crash).  The port
# raises DemangleError.
GO_PANICS = [
    "_ZW1McvfpT",  # nil pointer dereference (ModuleName with nil Parent)
    "_GLOBAL__I__Z1bW1McvsZvGr5_a$SGRd0_",  # nil pointer dereference
    "_ZNDTrQ",  # panic("internal error") in st.advance
    "_Z1aIXrQi",  # panic("internal error") in st.advance
    "_ZNFcv3foo",  # interface conversion: *Friend is not *Cast
    "_ZNW1McvclDKDFi",  # interface conversion: *ModuleEntity is not *Cast
    "_ZN0_17h0123456789abcdefE",  # slice bounds out of range (old Rust)
]


@pytest.mark.parametrize("name", GO_PANICS)
def test_go_panics_raise_demangle_error(name):
    with pytest.raises(DemangleError):
        to_string(name)


def test_not_mangled():
    for name in ("", "foo", "_Z", "_R", "___Zfoo", "_GLOBAL_"):
        with pytest.raises(DemangleError):
            to_string(name)
    with pytest.raises(NotMangledNameError):
        to_string("main")


def test_non_ascii_is_handled_bytewise():
    # Go indexes bytes: "4föo" is the 4-byte identifier "föo".
    assert to_string("_Z4föov") == "föo()"
    # "3föo" takes the two bytes of "ö"; the "o" left over is a type.
    assert to_string("_Z3föov") == "fö(unsigned __int128, void)"


def test_deep_nesting_does_not_hit_recursion_limit():
    # Far deeper than the default Python recursion limit (and than any real
    # symbol). Python 3.10/3.11 use the C stack for each Python call, so they
    # can't go as deep as 3.12+.
    depth = 200_000 if sys.version_info >= (3, 12) else 20_000
    assert to_string("_Z1f" + "P" * depth + "i") == "f(int" + "*" * depth + ")"


def test_too_deep_raises_demangle_error(monkeypatch):
    monkeypatch.setattr(demangle_pkg, "_DEEP_RECURSION_LIMIT", 3000)
    with pytest.raises(DemangleError):
        to_string("_Z1f" + "P" * 20_000 + "i")


def test_random_input_only_raises_demangle_error():
    rng = random.Random(12345)
    alphabet = "_0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.$"
    prefixes = ["_Z", "_ZN", "_ZZ", "_ZT", "_ZG", "_R", "_RN", "_GLOBAL__I_", "___Z"]
    for _ in range(20_000):
        name = rng.choice(prefixes) + "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 30)))
        try:
            assert isinstance(to_string(name), str)
        except DemangleError:
            pass


def _dynsym_names(path: Path) -> list:
    """Names of the .dynsym entries of a 32-bit little-endian ELF file."""
    data = path.read_bytes()
    assert data[:6] == b"\x7fELF\x01\x01"
    shoff = struct.unpack_from("<I", data, 0x20)[0]
    shentsize, shnum = struct.unpack_from("<HH", data, 0x2E)
    secs = [struct.unpack_from("<10I", data, shoff + i * shentsize) for i in range(shnum)]
    names = []
    for sh in secs:
        if sh[1] == 11:  # SHT_DYNSYM
            stroff = secs[sh[6]][4]
            for j in range(sh[5] // 16):
                st_name = struct.unpack_from("<I", data, sh[4] + j * 16)[0]
                if st_name:
                    end = data.index(b"\0", stroff + st_name)
                    names.append(data[stroff + st_name : end].decode("utf-8", "surrogateescape"))
    return names


def test_performance_libnickel():
    names = _dynsym_names(TESTDATA / "libnickel.so.1.0.0")
    assert len(names) > 40_000
    t0 = time.perf_counter()
    for name in names:
        try:
            to_string(name)
        except DemangleError:
            pass
    elapsed = time.perf_counter() - t0
    assert elapsed < 10.0, "demangling %d names took %.1fs" % (len(names), elapsed)
