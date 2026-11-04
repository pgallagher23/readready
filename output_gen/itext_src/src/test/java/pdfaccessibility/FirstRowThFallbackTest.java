// SPDX-License-Identifier: AGPL-3.0-or-later
package pdfaccessibility;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.ArrayList;
import java.util.List;
import java.util.TreeMap;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import com.google.gson.JsonObject;
import pdfaccessibility.ContentPreservingTagger.CellInfo;

/**
 * First-row-TH fallback (Ally "TableHeaders"): a table that reaches the
 * tagger with ZERO header flags anywhere and >=2 rows gets its first row
 * promoted to column headers. Tables with ANY header flag, single-row
 * tables, and runs with the flag disabled must be left untouched.
 */
public class FirstRowThFallbackTest {

    private boolean savedFlag;

    @BeforeEach
    void saveFlag() { savedFlag = PdfAccessibilityTagger.firstRowThFallback; }

    @AfterEach
    void restoreFlag() { PdfAccessibilityTagger.firstRowThFallback = savedFlag; }

    private static CellInfo cell(int row, int col, boolean isHeader,
            boolean isColHeader, boolean isRowHeader) {
        return cellWithText(row, col, isHeader, isColHeader, isRowHeader,
            "c" + row + col);
    }

    private static CellInfo cellWithText(int row, int col, boolean isHeader,
            boolean isColHeader, boolean isRowHeader, String text) {
        return new CellInfo(row, col, 1, 1, text,
            isHeader, isColHeader, isRowHeader, new ArrayList<>(), 1);
    }

    private static TreeMap<Integer, List<CellInfo>> table(CellInfo... cells) {
        TreeMap<Integer, List<CellInfo>> rows = new TreeMap<>();
        for (CellInfo c : cells) {
            rows.computeIfAbsent(c.row, k -> new ArrayList<>()).add(c);
        }
        return rows;
    }

    @Test
    void zeroHeaderTablePromotesFirstRowToColumnHeaders() {
        TreeMap<Integer, List<CellInfo>> rows = table(
            cell(0, 0, false, false, false), cell(0, 1, false, false, false),
            cell(1, 0, false, false, false), cell(1, 1, false, false, false));
        assertTrue(ContentPreservingTagger.promoteFirstRowHeaders(rows));
        for (CellInfo c : rows.get(0)) assertTrue(c.isColHeader);
        for (CellInfo c : rows.get(1)) assertFalse(c.isColHeader);
    }

    @Test
    void anyHeaderFlagAnywhereBlocksPromotion() {
        // Row-header in the body: table already has header semantics.
        TreeMap<Integer, List<CellInfo>> rows = table(
            cell(0, 0, false, false, false), cell(0, 1, false, false, false),
            cell(1, 0, false, false, true),  cell(1, 1, false, false, false));
        assertFalse(ContentPreservingTagger.promoteFirstRowHeaders(rows));
        for (CellInfo c : rows.get(0)) assertFalse(c.isColHeader);
    }

    @Test
    void partialFirstRowHeaderBlocksPromotion() {
        TreeMap<Integer, List<CellInfo>> rows = table(
            cell(0, 0, false, true, false), cell(0, 1, false, false, false),
            cell(1, 0, false, false, false), cell(1, 1, false, false, false));
        assertFalse(ContentPreservingTagger.promoteFirstRowHeaders(rows));
        assertFalse(rows.get(0).get(1).isColHeader);
    }

    @Test
    void singleRowTableIsNotPromoted() {
        TreeMap<Integer, List<CellInfo>> rows = table(
            cell(0, 0, false, false, false), cell(0, 1, false, false, false));
        assertFalse(ContentPreservingTagger.promoteFirstRowHeaders(rows));
        for (CellInfo c : rows.get(0)) assertFalse(c.isColHeader);
    }

    @Test
    void disabledFlagBlocksPromotion() {
        PdfAccessibilityTagger.firstRowThFallback = false;
        TreeMap<Integer, List<CellInfo>> rows = table(
            cell(0, 0, false, false, false),
            cell(1, 0, false, false, false));
        assertFalse(ContentPreservingTagger.promoteFirstRowHeaders(rows));
        assertFalse(rows.get(0).get(0).isColHeader);
    }

    @Test
    void allEmptyFirstRowNotPromoted() {
        // Gap-fill synthesizes an all-empty row 0 when the source table's
        // real cells start at row index >=1. Must not promote filler cells.
        TreeMap<Integer, List<CellInfo>> rows = table(
            cellWithText(0, 0, false, false, false, ""),
            cellWithText(0, 1, false, false, false, "   "),
            cell(1, 0, false, false, false), cell(1, 1, false, false, false));
        assertFalse(ContentPreservingTagger.promoteFirstRowHeaders(rows));
        for (CellInfo c : rows.get(0)) assertFalse(c.isColHeader);
    }

    private static JsonObject jsonCell(int row, int col,
            Boolean isHeader, Boolean isColHeader, Boolean isRowHeader) {
        return jsonCellWithText(row, col, isHeader, isColHeader, isRowHeader,
            "c" + row + col);
    }

    private static JsonObject jsonCellWithText(int row, int col,
            Boolean isHeader, Boolean isColHeader, Boolean isRowHeader,
            String text) {
        JsonObject c = new JsonObject();
        c.addProperty("row", row);
        c.addProperty("col", col);
        if (text != null) c.addProperty("text", text);
        if (isHeader != null) c.addProperty("is_header", isHeader);
        if (isColHeader != null) c.addProperty("is_col_header", isColHeader);
        if (isRowHeader != null) c.addProperty("is_row_header", isRowHeader);
        return c;
    }

    private static TreeMap<Integer, List<JsonObject>> jsonTable(JsonObject... cells) {
        TreeMap<Integer, List<JsonObject>> rows = new TreeMap<>();
        for (JsonObject c : cells) {
            rows.computeIfAbsent(c.get("row").getAsInt(),
                k -> new ArrayList<>()).add(c);
        }
        return rows;
    }

    @Test
    void jsonZeroHeaderTablePromotesFirstRow() {
        // Flags absent entirely — the common Docling no-header shape.
        TreeMap<Integer, List<JsonObject>> rows = jsonTable(
            jsonCell(0, 0, null, null, null), jsonCell(0, 1, null, null, null),
            jsonCell(1, 0, null, null, null), jsonCell(1, 1, null, null, null));
        assertTrue(PdfAccessibilityTagger.promoteFirstRowHeadersJson(rows));
        for (JsonObject c : rows.get(0)) {
            assertTrue(c.get("is_col_header").getAsBoolean());
        }
        for (JsonObject c : rows.get(1)) {
            assertFalse(c.has("is_col_header")
                && c.get("is_col_header").getAsBoolean());
        }
    }

    @Test
    void jsonAnyHeaderFlagBlocksPromotion() {
        TreeMap<Integer, List<JsonObject>> rows = jsonTable(
            jsonCell(0, 0, null, null, null),
            jsonCell(1, 0, null, null, true));
        assertFalse(PdfAccessibilityTagger.promoteFirstRowHeadersJson(rows));
        assertFalse(rows.get(0).get(0).has("is_col_header")
            && rows.get(0).get(0).get("is_col_header").getAsBoolean());
    }

    @Test
    void jsonExplicitFalseFlagsStillPromote() {
        // Gap-filler cells carry explicit false flags — must not block.
        TreeMap<Integer, List<JsonObject>> rows = jsonTable(
            jsonCell(0, 0, false, false, false),
            jsonCell(1, 0, false, false, false));
        assertTrue(PdfAccessibilityTagger.promoteFirstRowHeadersJson(rows));
        assertTrue(rows.get(0).get(0).get("is_col_header").getAsBoolean());
    }

    @Test
    void jsonSingleRowNotPromoted() {
        TreeMap<Integer, List<JsonObject>> rows = jsonTable(
            jsonCell(0, 0, null, null, null), jsonCell(0, 1, null, null, null));
        assertFalse(PdfAccessibilityTagger.promoteFirstRowHeadersJson(rows));
    }

    @Test
    void jsonAllEmptyFirstRowNotPromoted() {
        // Gap-fill synthesizes an all-empty row 0 (text absent or blank)
        // when the source table's real cells start at row index >=1.
        TreeMap<Integer, List<JsonObject>> rows = jsonTable(
            jsonCellWithText(0, 0, null, null, null, null),
            jsonCellWithText(0, 1, null, null, null, "  "),
            jsonCell(1, 0, null, null, null), jsonCell(1, 1, null, null, null));
        assertFalse(PdfAccessibilityTagger.promoteFirstRowHeadersJson(rows));
        for (JsonObject c : rows.get(0)) {
            assertFalse(c.has("is_col_header")
                && c.get("is_col_header").getAsBoolean());
        }
    }

    @Test
    void jsonDisabledFlagBlocksPromotion() {
        PdfAccessibilityTagger.firstRowThFallback = false;
        TreeMap<Integer, List<JsonObject>> rows = jsonTable(
            jsonCell(0, 0, null, null, null),
            jsonCell(1, 0, null, null, null));
        assertFalse(PdfAccessibilityTagger.promoteFirstRowHeadersJson(rows));
    }
}
