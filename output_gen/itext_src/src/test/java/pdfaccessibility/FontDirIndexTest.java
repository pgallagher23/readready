// SPDX-License-Identifier: AGPL-3.0-or-later
package pdfaccessibility;

import static org.junit.jupiter.api.Assertions.*;
import java.io.File;
import java.nio.file.Files;
import java.nio.file.Path;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

class FontDirIndexTest {
    // Surefire runs every test class in one JVM; other classes may already have
    // cached resolvedDir, so reset before AND after each test.
    @BeforeEach void reset() { System.clearProperty("readready.fontDir"); FontDirIndex.clearCache(); }
    @AfterEach void clear() { System.clearProperty("readready.fontDir"); FontDirIndex.clearCache(); }

    @Test void systemPropertyWins() throws Exception {
        Path d = Files.createTempDirectory("fonts");
        System.setProperty("readready.fontDir", d.toString());
        assertEquals(d.toString() + "/", FontDirIndex.resolveFontDir());
    }

    @Test void resolveIsCaseInsensitive() throws Exception {
        Path d = Files.createTempDirectory("fonts");
        Files.write(d.resolve("ARIALBD.TTF"), new byte[]{0});
        FontDirIndex idx = FontDirIndex.of(d.toString() + "/");
        assertEquals(d.resolve("ARIALBD.TTF").toString(), idx.resolve("arialbd.ttf"));
        assertNull(idx.resolve("times.ttf"));
    }

    @Test void missingDirectoryYieldsEmptyIndexNotException() {
        FontDirIndex idx = FontDirIndex.of("/nonexistent/fonts/");
        assertNull(idx.resolve("arial.ttf"));
    }

    @Test void mapToWindowsFontReturnsRealPathWhenPresent() throws Exception {
        Path d = Files.createTempDirectory("fonts");
        Files.write(d.resolve("Times.TTF"), new byte[]{0});
        System.setProperty("readready.fontDir", d.toString());
        assertEquals(d.resolve("Times.TTF").toString(),
            PdfAccessibilityTagger.mapToWindowsFont("TimesNewRomanPSMT", false, false));
    }
}
