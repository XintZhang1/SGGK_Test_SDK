#include <Foundation/init.h>
#include <Topology/Brep/Body.h>
#include <Topology/Brep/Coedge.h>
#include <Topology/Brep/Edge.h>
#include <Topology/Brep/Face.h>
#include <Topology/Brep/Lump.h>
#include <Topology/Brep/Shell.h>
#include <Topology/Brep/Topology.h>
#include <Topology/Brep/Vertex.h>
#include <Topology/Brep/Wire.h>
#include <Topology/Serialize/RapidTopoJsonDeserializer.h>
#include <Topology/Serialize/RapidTopoJsonSerializer.h>

#include <algorithm>
#include <cctype>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace fs = std::filesystem;

namespace
{
struct Options
{
    fs::path source;
    fs::path out;
    std::string type;
    bool idSet = false;
    sggk::ID id = 0;
    int localIndex = -1;
    int bodyIndex = 0;
    std::string label;
};

struct Selection
{
    sggk::TopologyPtr topology;
    int localIndex = -1;
    std::string matchMode;
};

std::string ToLower(std::string text)
{
    std::transform(text.begin(), text.end(), text.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return text;
}

std::string EscapeJson(const std::string& value)
{
    std::ostringstream os;
    for (const char ch : value)
    {
        switch (ch)
        {
        case '\\': os << "\\\\"; break;
        case '"': os << "\\\""; break;
        case '\n': os << "\\n"; break;
        case '\r': os << "\\r"; break;
        case '\t': os << "\\t"; break;
        default: os << ch; break;
        }
    }
    return os.str();
}

std::string SanitizeName(std::string value)
{
    for (char& ch : value)
    {
        const unsigned char c = static_cast<unsigned char>(ch);
        if (!std::isalnum(c) && ch != '_' && ch != '-' && ch != '.')
        {
            ch = '_';
        }
    }
    while (!value.empty() && (value.front() == '.' || value.front() == '_'))
    {
        value.erase(value.begin());
    }
    return value.empty() ? "topology" : value;
}

Options ParseArgs(int argc, char** argv)
{
    Options opts;
    for (int i = 1; i < argc; ++i)
    {
        const std::string arg = argv[i];
        auto requireValue = [&](const std::string& name) -> std::string {
            if (i + 1 >= argc)
            {
                throw std::runtime_error(name + " requires a value");
            }
            return argv[++i];
        };
        if (arg == "--source")
        {
            opts.source = requireValue(arg);
        }
        else if (arg == "--out")
        {
            opts.out = requireValue(arg);
        }
        else if (arg == "--type")
        {
            opts.type = requireValue(arg);
        }
        else if (arg == "--id")
        {
            opts.id = static_cast<sggk::ID>(std::stoull(requireValue(arg)));
            opts.idSet = true;
        }
        else if (arg == "--local-index")
        {
            opts.localIndex = std::stoi(requireValue(arg));
        }
        else if (arg == "--body-index")
        {
            opts.bodyIndex = std::stoi(requireValue(arg));
        }
        else if (arg == "--label")
        {
            opts.label = requireValue(arg);
        }
        else if (arg == "--help" || arg == "-h")
        {
            std::cout
                << "Usage: sggk_topology_extract --source input.sgt --out out-dir-or-file "
                << "--type Body|Face|Edge|Vertex|Wire|Shell|Lump|Coedge [--id N] [--local-index N] [--body-index N] [--label name]\n";
            std::exit(0);
        }
        else
        {
            throw std::runtime_error("unknown argument: " + arg);
        }
    }
    if (opts.source.empty())
    {
        throw std::runtime_error("--source is required");
    }
    if (opts.out.empty())
    {
        throw std::runtime_error("--out is required");
    }
    if (opts.type.empty())
    {
        throw std::runtime_error("--type is required");
    }
    if (!opts.idSet && opts.localIndex < 0)
    {
        throw std::runtime_error("pass --id, --local-index, or both");
    }
    return opts;
}

std::vector<sggk::TopologyPtr> CollectByType(const sggk::BodyPtr& body, const std::string& type)
{
    std::vector<sggk::TopologyPtr> result;
    const std::string normalized = ToLower(type);
    if (normalized == "body")
    {
        result.push_back(body);
    }
    else if (normalized == "lump")
    {
        for (const auto& topo : body->Lumps()) result.push_back(topo);
    }
    else if (normalized == "shell")
    {
        for (const auto& topo : body->QueryShells()) result.push_back(topo);
    }
    else if (normalized == "face")
    {
        for (const auto& topo : body->QueryFaces()) result.push_back(topo);
    }
    else if (normalized == "wire")
    {
        for (const auto& topo : body->QueryWires()) result.push_back(topo);
    }
    else if (normalized == "coedge")
    {
        for (const auto& topo : body->QueryCoedges()) result.push_back(topo);
    }
    else if (normalized == "edge")
    {
        for (const auto& topo : body->QueryEdges()) result.push_back(topo);
    }
    else if (normalized == "vertex")
    {
        for (const auto& topo : body->QueryVertices()) result.push_back(topo);
    }
    else
    {
        throw std::runtime_error("unsupported topology type: " + type);
    }
    return result;
}

fs::path OutputPath(const Options& opts, const sggk::TopologyPtr& selected)
{
    if (opts.out.extension() == ".sgt")
    {
        return opts.out;
    }
    const std::string label = opts.label.empty()
        ? opts.source.stem().string() + "_" + ToLower(opts.type) + "_" + std::to_string(static_cast<unsigned long long>(selected->ID()))
        : opts.label;
    return opts.out / (SanitizeName(label) + ".sgt");
}

Selection SelectTopology(const std::vector<sggk::TopologyPtr>& topologies, const Options& opts)
{
    auto findBy = [&](bool requireId, bool requireLocal, const std::string& mode) -> Selection {
        for (size_t i = 0; i < topologies.size(); ++i)
        {
            const auto& topo = topologies[i];
            if (!topo)
            {
                continue;
            }
            const bool idMatch = !requireId || (opts.idSet && topo->ID() == opts.id);
            const bool localMatch = !requireLocal || (opts.localIndex >= 0 && static_cast<int>(i) == opts.localIndex);
            if (idMatch && localMatch)
            {
                return Selection{topo, static_cast<int>(i), mode};
            }
        }
        return Selection{};
    };

    if (opts.idSet && opts.localIndex >= 0)
    {
        Selection selected = findBy(true, true, "id_and_local_index");
        if (selected.topology) return selected;
        selected = findBy(false, true, "local_index_fallback");
        if (selected.topology) return selected;
        selected = findBy(true, false, "id_fallback");
        if (selected.topology) return selected;
    }
    if (opts.localIndex >= 0)
    {
        Selection selected = findBy(false, true, "local_index");
        if (selected.topology) return selected;
    }
    if (opts.idSet)
    {
        Selection selected = findBy(true, false, "id");
        if (selected.topology) return selected;
    }
    return Selection{};
}

void WriteManifest(
    const fs::path& path,
    const Options& opts,
    const fs::path& output,
    const sggk::TopologyPtr& selected,
    int selectedLocalIndex,
    const std::string& matchMode)
{
    std::ostringstream os;
    os << "{\n"
       << "  \"source\": \"" << EscapeJson(fs::absolute(opts.source).string()) << "\",\n"
       << "  \"output\": \"" << EscapeJson(fs::absolute(output).string()) << "\",\n"
       << "  \"type\": \"" << EscapeJson(opts.type) << "\",\n"
       << "  \"id\": " << static_cast<unsigned long long>(selected->ID()) << ",\n"
       << "  \"local_index\": " << selectedLocalIndex << ",\n"
       << "  \"requested_id\": " << (opts.idSet ? std::to_string(static_cast<unsigned long long>(opts.id)) : std::string("null")) << ",\n"
       << "  \"requested_local_index\": " << opts.localIndex << ",\n"
       << "  \"match_mode\": \"" << EscapeJson(matchMode) << "\",\n"
       << "  \"body_index\": " << opts.bodyIndex << "\n"
       << "}\n";
    std::ofstream file(path, std::ios::binary);
    file << os.str();
}

int Run(const Options& opts)
{
    if (!fs::is_regular_file(opts.source))
    {
        throw std::runtime_error("source SGT not found: " + opts.source.string());
    }

    sggk::RapidTopoJsonDeserializer deserializer;
    auto bodies = deserializer.DeserializeBodiesFromFile(opts.source.string().c_str());
    if (bodies.empty())
    {
        auto body = deserializer.DeserializeBodyFromFile(opts.source.string().c_str());
        if (body)
        {
            bodies.push_back(body);
        }
    }
    if (bodies.empty())
    {
        throw std::runtime_error("no bodies in source SGT: " + opts.source.string());
    }
    if (opts.bodyIndex < 0 || static_cast<size_t>(opts.bodyIndex) >= bodies.size())
    {
        throw std::runtime_error("body index out of range");
    }

    const auto topologies = CollectByType(bodies[static_cast<size_t>(opts.bodyIndex)], opts.type);
    const Selection selection = SelectTopology(topologies, opts);
    if (!selection.topology)
    {
        std::ostringstream msg;
        msg << "topology not found: type=" << opts.type;
        if (opts.idSet) msg << " id=" << static_cast<unsigned long long>(opts.id);
        if (opts.localIndex >= 0) msg << " local_index=" << opts.localIndex;
        throw std::runtime_error(msg.str());
    }

    const fs::path output = OutputPath(opts, selection.topology);
    if (!output.parent_path().empty())
    {
        fs::create_directories(output.parent_path());
    }
    sggk::RapidTopoJsonSerializer serializer;
    serializer.Serialize(selection.topology, output.string().c_str());
    WriteManifest(
        output.parent_path() / (output.stem().string() + ".manifest.json"),
        opts,
        output,
        selection.topology,
        selection.localIndex,
        selection.matchMode);

    std::cout << "source=" << fs::absolute(opts.source).string() << "\n"
              << "output=" << fs::absolute(output).string() << "\n"
              << "type=" << opts.type << "\n"
              << "id=" << static_cast<unsigned long long>(selection.topology->ID()) << "\n"
              << "local_index=" << selection.localIndex << "\n"
              << "match_mode=" << selection.matchMode << "\n";
    return 0;
}
}

int main(int argc, char** argv)
{
    try
    {
        const Options opts = ParseArgs(argc, argv);
        sggk::init(nullptr, 16);
        const int code = Run(opts);
        sggk::fini();
        return code;
    }
    catch (const std::exception& ex)
    {
        std::cerr << "sggk_topology_extract: " << ex.what() << "\n";
        return 1;
    }
}
