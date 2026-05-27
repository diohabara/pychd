from pychd.validate import compare_ast, normalize_ast, validate, validate_directory


class TestNormalizeAst:
    def test_strips_line_numbers(self):
        import ast

        source = "x = 1\ny = 2\n"
        tree = normalize_ast(source)
        for node in ast.walk(tree):
            assert getattr(node, "lineno", None) is None
            assert getattr(node, "col_offset", None) is None


class TestCompareAst:
    def test_identical_sources_match(self):
        source = "x = 1\ny = x + 2\n"
        result = compare_ast(source, source)
        assert result.match is True

    def test_different_sources_differ(self):
        result = compare_ast("x = 1\n", "x = 2\n")
        assert result.match is False

    def test_same_semantics_different_whitespace(self):
        a = "x=1\ny=2\n"
        b = "x = 1\ny = 2\n"
        result = compare_ast(a, b)
        assert result.match is True

    def test_syntax_error_returns_no_match(self):
        result = compare_ast("def :", "x = 1\n")
        assert result.match is False
        assert "Failed to parse" in result.details


class TestValidate:
    def test_matching_files(self, tmp_path):
        a = tmp_path / "a.py"
        b = tmp_path / "b.py"
        a.write_text("x = 1\n")
        b.write_text("x = 1\n")
        result = validate(a, b)
        assert result.match is True

    def test_differing_files(self, tmp_path):
        a = tmp_path / "a.py"
        b = tmp_path / "b.py"
        a.write_text("x = 1\n")
        b.write_text("x = 99\n")
        result = validate(a, b)
        assert result.match is False


class TestValidateDirectory:
    def test_directory_comparison(self, tmp_path):
        orig = tmp_path / "orig"
        decomp = tmp_path / "decomp"
        orig.mkdir()
        decomp.mkdir()
        (orig / "a.py").write_text("x = 1\n")
        (decomp / "a.py").write_text("x = 1\n")
        (orig / "b.py").write_text("y = 2\n")
        (decomp / "b.py").write_text("y = 99\n")
        results = validate_directory(orig, decomp)
        assert len(results) == 2
        matches = [r for _, r in results if r.match]
        assert len(matches) == 1

    def test_missing_decompiled_file(self, tmp_path):
        orig = tmp_path / "orig"
        decomp = tmp_path / "decomp"
        orig.mkdir()
        decomp.mkdir()
        (orig / "a.py").write_text("x = 1\n")
        results = validate_directory(orig, decomp)
        assert len(results) == 1
        assert results[0][1].match is False
        assert "Missing" in results[0][1].details
