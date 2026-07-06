"""核型解析器測試 — 涵蓋 SCREENER_REFERENCE.md 的解析範例表。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ipssm_pipeline import parse_karyotype


def test_normal_male():
    r = parse_karyotype("46,XY")
    assert r == {'del5q': 0, 'del7_7q': 0, 'del17p': 0, 'complex': 0, 'cyto_ipssr': 'Good'}


def test_del5q_is_good():
    r = parse_karyotype("46,XY,del(5q)")
    assert r['del5q'] == 1
    assert r['del7_7q'] == 0
    assert r['cyto_ipssr'] == 'Good'


def test_minus7_is_poor():
    r = parse_karyotype("46,XY,-7")
    assert r['del7_7q'] == 1
    assert r['cyto_ipssr'] == 'Poor'


def test_minus_y_is_very_good():
    r = parse_karyotype("45,XY,-Y")
    assert r['cyto_ipssr'] == 'Very Good'


def test_complex_karyotype_flag():
    r = parse_karyotype("47,XY,+8,del(5q),del(7q)")
    assert r['del5q'] == 1
    assert r['del7_7q'] == 1
    assert r['complex'] == 1


def test_nm_returns_na():
    r = parse_karyotype("NM")
    assert r['cyto_ipssr'] == 'NA'


def test_none_and_empty():
    for val in (None, "", float('nan')):
        r = parse_karyotype(val)
        assert r['del5q'] == 0
        assert r['cyto_ipssr'] == 'NA'
