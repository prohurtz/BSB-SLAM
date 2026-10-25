// Test-only vrpathreg replacement. It writes exclusively beneath redirected LOCALAPPDATA.
using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Web.Script.Serialization;

public static class RegistryFixture {
    public static int Main(string[] args) {
        if (args.Length != 2 || (args[0] != "adddriver" && args[0] != "removedriver")) return 1;
        string path = Path.Combine(Environment.GetEnvironmentVariable("LOCALAPPDATA"), "openvr", "openvrpaths.vrpath");
        var json = new JavaScriptSerializer();
        var document = File.Exists(path) ? json.Deserialize<Dictionary<string, object>>(File.ReadAllText(path)) : new Dictionary<string, object>();
        var drivers = new List<string>();
        if (document.ContainsKey("external_drivers"))
            foreach (object item in (IEnumerable)document["external_drivers"])
                if (!String.Equals(Path.GetFullPath(item.ToString()), Path.GetFullPath(args[1]), StringComparison.OrdinalIgnoreCase)) drivers.Add(item.ToString());
        if (args[0] == "adddriver") drivers.Add(args[1]);
        document["external_drivers"] = drivers;
        Directory.CreateDirectory(Path.GetDirectoryName(path));
        File.WriteAllText(path, json.Serialize(document));
        return 0;
    }
}
