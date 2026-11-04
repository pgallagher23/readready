// SPDX-License-Identifier: AGPL-3.0-or-later
package pdfaccessibility;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;

/**
 * Unit tests for {@link ContentPreservingTagger#forceInvisibleWarning}.
 *
 * On invisible-only pages the editor forces "3 Tr" at the start of every
 * marked-content scope. If that write fails, the OCR text layer becomes
 * VISIBLE — black text painted over the scanned page image. That must never
 * be silent, so the failure is counted and reported once per page.
 */
class ForceInvisibleWarningTest {

    @Test
    void noWarningWhenNothingFailed() {
        assertNull(ContentPreservingTagger.forceInvisibleWarning(3, 0));
    }

    @Test
    void negativeCountIsTreatedAsNoFailure() {
        assertNull(ContentPreservingTagger.forceInvisibleWarning(3, -1));
    }

    @Test
    void warningNamesThePageAndTheCount() {
        String msg = ContentPreservingTagger.forceInvisibleWarning(7, 4);
        assertTrue(msg.contains("Page 7"), msg);
        assertTrue(msg.contains("4"), msg);
    }

    @Test
    void warningStatesTheVisibleConsequence() {
        String msg = ContentPreservingTagger.forceInvisibleWarning(1, 1);
        assertTrue(msg.toUpperCase().contains("WARNING"), msg);
        assertTrue(msg.toLowerCase().contains("visib"), msg);
    }

    @Test
    void warningIsASingleLine() {
        String msg = ContentPreservingTagger.forceInvisibleWarning(2, 9);
        assertEquals(-1, msg.indexOf('\n'), "warning must be one line: " + msg);
    }
}
