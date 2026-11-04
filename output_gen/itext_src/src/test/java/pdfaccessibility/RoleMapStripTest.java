// SPDX-License-Identifier: AGPL-3.0-or-later
package pdfaccessibility;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;

import org.junit.jupiter.api.Test;

import com.itextpdf.kernel.pdf.PdfDictionary;
import com.itextpdf.kernel.pdf.PdfDocument;
import com.itextpdf.kernel.pdf.PdfName;
import com.itextpdf.kernel.pdf.PdfReader;
import com.itextpdf.kernel.pdf.PdfWriter;

/**
 * The RoleMap inherited from a source document must not survive a struct-tree
 * rebuild. A stale entry whose key is a standard type (Word emits /Caption /P)
 * makes veraPDF fail ISO 14289-1 7.1 t7, and cascades into 7.2 t3 when the
 * remapped tag is a table child.
 *
 * Assertions are made on the REOPENED bytes, not the in-memory dictionary
 * produced immediately after the strip call — the point is to prove the
 * mapping is gone from the file iText actually wrote, not just from the
 * object graph we happened to be holding a reference to.
 *
 * NOTE on what "has a RoleMap" means here: {@code hasRoleMap} checks for a
 * NON-EMPTY RoleMap, not mere key presence. Reopening ANY tagged PDF via
 * {@code new PdfDocument(new PdfReader(...))} unconditionally constructs a
 * {@code PdfStructTreeRoot} wrapper, and that wrapper's constructor
 * self-heals an empty {@code /RoleMap} into the in-memory dictionary
 * whenever one is absent ("Always init role map dictionary in order to
 * avoid inconsistency" — see {@code PdfStructTreeRoot#getRoleMap}). That
 * happens on every reopen, including a genuinely-stripped file, so
 * {@code get(RoleMap) != null} is never false after a round-trip and cannot
 * distinguish "stripped" from "not stripped". Confirmed empirically with a
 * diagnostic build of iText 9.5.0 kernel (stack trace on the self-heal
 * branch) plus a raw-byte scan of the written PDF: a correctly-stripped
 * file contains zero occurrences of the literal token "/RoleMap" on disk,
 * even though the reopened in-memory dictionary reports an (empty) one.
 * What actually causes the veraPDF violation this strip prevents is a
 * MAPPED entry such as /Caption /P; an empty map maps nothing and is
 * harmless, so checking for non-emptiness is the correct, meaningful
 * signal — and it still exercises the real read-back path end to end.
 */
public class RoleMapStripTest {

    /** A tagged one-page PDF whose StructTreeRoot carries /Caption /P. */
    private static byte[] pdfWithRoleMap() {
        ByteArrayOutputStream baos = new ByteArrayOutputStream();
        try (PdfDocument doc = new PdfDocument(new PdfWriter(baos))) {
            doc.setTagged();
            doc.addNewPage();
            PdfDictionary roleMap = new PdfDictionary();
            roleMap.put(new PdfName("Caption"), new PdfName("P"));
            roleMap.put(new PdfName("Normal"), new PdfName("P"));
            doc.getStructTreeRoot().getPdfObject().put(PdfName.RoleMap, roleMap);
        }
        return baos.toByteArray();
    }

    /**
     * Reopen the bytes and report whether the StructTreeRoot carries a
     * RoleMap with at least one entry. See the class Javadoc for why
     * emptiness, not mere presence, is the check that means something here.
     * The answer is computed INSIDE the try-with-resources — iText releases
     * indirect objects on close, so a PdfDictionary read after the document
     * closes is not safe to inspect.
     */
    private static boolean hasRoleMap(byte[] pdf) throws IOException {
        try (PdfDocument doc = new PdfDocument(
                new PdfReader(new ByteArrayInputStream(pdf)))) {
            PdfDictionary stRoot = doc.getCatalog().getPdfObject()
                .getAsDictionary(PdfName.StructTreeRoot);
            assertNotNull(stRoot, "document must have a StructTreeRoot");
            PdfDictionary roleMap = stRoot.getAsDictionary(PdfName.RoleMap);
            return roleMap != null && !roleMap.isEmpty();
        }
    }

    /**
     * Guard: if the fixture ever stops carrying a RoleMap, the strip test
     * below would pass vacuously.
     */
    @Test
    void fixtureActuallyCarriesARoleMap() throws IOException {
        assertTrue(hasRoleMap(pdfWithRoleMap()),
            "fixture must carry a RoleMap or the strip test proves nothing");
    }

    @Test
    void inheritedRoleMapDoesNotSurviveTheRebuild() throws IOException {
        byte[] src = pdfWithRoleMap();
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        try (PdfDocument doc = new PdfDocument(
                new PdfReader(new ByteArrayInputStream(src)), new PdfWriter(out))) {
            doc.setTagged();
            PdfAccessibilityTagger.stripInheritedRoleMap(
                doc.getCatalog().getPdfObject()
                    .getAsDictionary(PdfName.StructTreeRoot));
        }
        assertFalse(hasRoleMap(out.toByteArray()),
            "a mapped RoleMap entry survived the rebuild in the written file");
    }

    @Test
    void nullStructTreeRootIsTolerated() {
        // Untagged source documents have no StructTreeRoot at all.
        assertDoesNotThrow(
            () -> PdfAccessibilityTagger.stripInheritedRoleMap(null));
    }
}
