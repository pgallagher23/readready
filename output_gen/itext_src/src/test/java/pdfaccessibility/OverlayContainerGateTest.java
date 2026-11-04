// SPDX-License-Identifier: AGPL-3.0-or-later
package pdfaccessibility;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.ArrayList;
import java.util.List;

import org.junit.jupiter.api.Test;

import pdfaccessibility.ContentPreservingTagger.CellInfo;
import pdfaccessibility.ContentPreservingTagger.ElementInfo;
import pdfaccessibility.ContentPreservingTagger.ListItemInfo;
import pdfaccessibility.ContentPreservingTagger.TocItemInfo;

/**
 * needsTag child-content extension: containers whose text lives ONLY in
 * children (Docling ListGroup -> L with empty top-level text) must still
 * be tagged, or overlay pages silently drop every list (seen on a batch: 20 docs,
 * ~66k chars).  hasChildContent() is the JUnit-visible core of that gate.
 */
public class OverlayContainerGateTest {

    private static ElementInfo elem(String tag, String text) {
        return new ElementInfo("e1", tag, text, null, 1);
    }

    @Test
    void listWithUsableChildTextHasChildContent() {
        ElementInfo info = elem("L", "");
        info.listItems = new ArrayList<>();
        info.listItems.add(new ListItemInfo("A real item.", null,
            new ArrayList<>()));
        assertTrue(ContentPreservingTagger.hasChildContent(info));
    }

    @Test
    void listWithOnlyWhitespaceChildrenHasNone() {
        ElementInfo info = elem("L", "");
        info.listItems = new ArrayList<>();
        info.listItems.add(new ListItemInfo("   ", null, new ArrayList<>()));
        assertFalse(ContentPreservingTagger.hasChildContent(info));
    }

    @Test
    void listLabelAloneCounts() {
        ElementInfo info = elem("L", "");
        info.listItems = new ArrayList<>();
        info.listItems.add(new ListItemInfo("", "1.", new ArrayList<>()));
        assertTrue(ContentPreservingTagger.hasChildContent(info));
    }

    @Test
    void tableCellTextCounts() {
        ElementInfo info = elem("Table", "");
        info.cells = new ArrayList<>();
        info.cells.add(new CellInfo(0, 0, 1, 1, "cell text",
            false, false, false, new ArrayList<>(), 1));
        assertTrue(ContentPreservingTagger.hasChildContent(info));
    }

    @Test
    void tocItemTextCounts() {
        ElementInfo info = elem("TOC", "");
        info.tocItems = new ArrayList<>();
        info.tocItems.add(new TocItemInfo("Chapter 1 .... 5",
            new ArrayList<>()));
        assertTrue(ContentPreservingTagger.hasChildContent(info));
    }

    @Test
    void plainParagraphWithoutChildrenHasNone() {
        assertFalse(ContentPreservingTagger.hasChildContent(elem("P", "")));
    }

    @Test
    void nullChildListsAreSafe() {
        ElementInfo info = elem("L", "");
        info.listItems = null;
        info.cells = null;
        info.tocItems = null;
        assertFalse(ContentPreservingTagger.hasChildContent(info));
    }
}
