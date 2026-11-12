import de.neemann.digital.draw.elements.Circuit;
import de.neemann.digital.draw.elements.Pin;
import de.neemann.digital.draw.elements.Pins;
import de.neemann.digital.draw.elements.VisualElement;
import de.neemann.digital.draw.elements.Wire;
import de.neemann.digital.draw.library.ElementLibrary;
import de.neemann.digital.draw.model.Net;
import de.neemann.digital.draw.model.NetList;
import de.neemann.digital.draw.shapes.ShapeFactory;

import java.io.File;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.PrintStream;
import java.util.ArrayList;
import java.util.IdentityHashMap;

/**
 * Ground truth from Digital itself - for every circuit file given, print one
 * JSON line with each element's real pin positions (after rotation and
 * mirroring) and the nets Digital builds from the wires and tunnels.
 *
 * usage: java -cp Digital.jar:. PinDump [--din] file.dig ...   (scripts/digital_oracle/oracle.py runs it)
 */
public class PinDump {

    private static String esc(String s) {
        if (s == null) return "";
        StringBuilder b = new StringBuilder();
        for (char c : s.toCharArray()) {
            if (c == '"' || c == '\\') b.append('\\').append(c);
            else if (c < 32) b.append(' ');
            else b.append(c);
        }
        return b.toString();
    }

    public static void main(String[] args) throws Exception {
        // UTF-8 whatever the console code page is: the caller decodes it as such
        PrintStream stdout = new PrintStream(new FileOutputStream(FileDescriptor.out), true, "UTF-8");
        boolean ieee = true;
        for (String a : args) {
            if (a.equals("--din")) { ieee = false; continue; }
            StringBuilder out = new StringBuilder();
            out.append("{\"file\":\"").append(esc(a)).append("\"");
            try {
                File f = new File(a).getAbsoluteFile();
                ElementLibrary library = new ElementLibrary();
                library.setRootFilePath(f.getParentFile());
                ShapeFactory sf = new ShapeFactory(library, ieee);
                Circuit c = Circuit.loadCircuit(f, sf);

                ArrayList<VisualElement> elements = c.getElements();
                IdentityHashMap<Pin, Integer> owner = new IdentityHashMap<>();
                ArrayList<Pins> allPins = new ArrayList<>();
                out.append(",\"elements\":[");
                for (int i = 0; i < elements.size(); i++) {
                    VisualElement ve = elements.get(i);
                    if (i > 0) out.append(',');
                    out.append("{\"i\":").append(i)
                       .append(",\"name\":\"").append(esc(ve.getElementName())).append("\"")
                       .append(",\"x\":").append(ve.getPos().x)
                       .append(",\"y\":").append(ve.getPos().y)
                       .append(",\"rot\":").append(ve.getRotate());
                    Pins pins;
                    try {
                        pins = ve.getPins();
                        out.append(",\"pins\":[");
                        boolean first = true;
                        for (Pin p : pins) {
                            owner.put(p, i);
                            if (!first) out.append(',');
                            first = false;
                            out.append("{\"n\":\"").append(esc(p.getName())).append("\"")
                               .append(",\"d\":\"").append(p.getDirection()).append("\"")
                               .append(",\"x\":").append(p.getPos().x)
                               .append(",\"y\":").append(p.getPos().y).append('}');
                        }
                        out.append(']');
                    } catch (Throwable t) {
                        pins = new Pins();
                        out.append(",\"pins\":[],\"pin_error\":\"").append(esc(String.valueOf(t))).append("\"");
                    }
                    allPins.add(pins);
                    out.append('}');
                }
                out.append(']');

                final StringBuilder netFallback = out;
                try {
                    NetList nl = new NetList(c);
                    for (Pins pins : allPins)
                        for (Pin p : pins)
                            nl.add(p);
                    StringBuilder main = out;
                    out = new StringBuilder();
                    out.append(",\"nets\":[");
                    boolean firstNet = true;
                    for (Net n : nl) {
                        if (!firstNet) out.append(',');
                        firstNet = false;
                        out.append("{\"points\":[");
                        boolean first = true;
                        ArrayList<Wire> ws = n.getWires();
                        if (ws != null) for (Wire w : ws) {
                            if (!first) out.append(',');
                            first = false;
                            out.append('[').append(w.p1.x).append(',').append(w.p1.y).append(',')
                               .append(w.p2.x).append(',').append(w.p2.y).append(']');
                        }
                        out.append("],\"pins\":[");
                        first = true;
                        for (Pin p : n.getPins()) {
                            Integer idx = owner.get(p);
                            if (!first) out.append(',');
                            first = false;
                            out.append('[').append(idx == null ? -1 : idx).append(",\"")
                               .append(esc(p.getName())).append("\",")
                               .append(p.getPos().x).append(',').append(p.getPos().y).append(']');
                        }
                        out.append("],\"labels\":[");
                        first = true;
                        java.util.HashSet<String> labels = n.getLabels();
                        if (labels != null) for (String l : labels) {
                            if (!first) out.append(',');
                            first = false;
                            out.append('"').append(esc(l)).append('"');
                        }
                        out.append("]}");
                    }
                    out.append(']');
                    main.append(out);
                    out = main;
                } catch (Throwable t) {
                    out = netFallback;
                    out.append(",\"nets\":null,\"net_error\":\"").append(esc(String.valueOf(t))).append("\"");
                }
            } catch (Throwable t) {
                out.append(",\"error\":\"").append(esc(String.valueOf(t))).append("\"");
            }
            out.append('}');
            stdout.println(out);
        }
    }
}
