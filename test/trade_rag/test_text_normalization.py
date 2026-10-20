from trade_rag.text_normalization import normalize_search_text


def test_nfkc_folds_cjk_compatibility_glyphs_for_search():
    assert normalize_search_text("⽆线路由器与2.5G⼝") == "无线路由器与2.5G口"
