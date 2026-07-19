// Deterministic mesh dump for the failure showcase: load .sgt bodies and
// tessellate every face into a single bounded JSON document.  Fixed CLI,
// no environment access; output feeds tools/render_mesh_views.py.
#include <Foundation/init.h>
#include <GeomBase/BndBox.h>
#include <Geometry/Mesh/GeomMeshEnums.h>
#include <Geometry/Mesh/SrfMeshOpt.h>
#include <Geometry/Mesh/SurfaceMesh.h>
#include <Geometry/Mesh/SurfaceMeshUtil.h>
#include <Geometry/3D/Surface/Surface.h>
#include <Topology/Brep/Body.h>
#include <Topology/Brep/Face.h>
#include <Topology/Serialize/RapidTopoJsonDeserializer.h>

#include <algorithm>
#include <cctype>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace fs = std::filesystem;

namespace
{
// Hard cap on triangles across all bodies in one invocation; excess faces are
// decimated with a deterministic stride so the JSON stays bounded.
const size_t kMaxTriangles = 120000;

struct Options
{
    fs::path outDir;
    std::vector<std::pair<std::string, fs::path>> bodies;
};

struct FaceMesh
{
    std::vector<double> verts;
    std::vector<unsigned int> tris;
};

struct BodyMesh
{
    std::string name;
    std::vector<FaceMesh> faces;
    size_t triangleCount = 0;
};

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
    return value.empty() ? "body" : value;
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
        if (arg == "--out")
        {
            opts.outDir = requireValue(arg);
        }
        else if (arg == "--body")
        {
            const std::string spec = requireValue(arg);
            const auto eq = spec.find('=');
            if (eq == std::string::npos || eq == 0 || eq + 1 >= spec.size())
            {
                throw std::runtime_error("--body expects <name>=<path.sgt>");
            }
            opts.bodies.emplace_back(spec.substr(0, eq), fs::path(spec.substr(eq + 1)));
        }
        else if (arg == "--help" || arg == "-h")
        {
            std::cout << "Usage: sggk_mesh_dump --out <dir> --body <name=path.sgt> [--body ...]\n";
            std::exit(0);
        }
        else
        {
            throw std::runtime_error("unknown argument: " + arg);
        }
    }
    if (opts.outDir.empty())
    {
        throw std::runtime_error("--out is required");
    }
    if (opts.bodies.empty())
    {
        throw std::runtime_error("at least one --body is required");
    }
    return opts;
}

std::vector<sggk::BodyPtr> LoadBodies(const fs::path& source)
{
    sggk::RapidTopoJsonDeserializer deserializer;
    auto bodies = deserializer.DeserializeBodiesFromFile(source.string().c_str());
    if (bodies.empty())
    {
        auto body = deserializer.DeserializeBodyFromFile(source.string().c_str());
        if (body)
        {
            bodies.push_back(body);
        }
    }
    if (bodies.empty())
    {
        throw std::runtime_error("no bodies in SGT: " + source.string());
    }
    return bodies;
}

double BodyDiagonal(const sggk::BodyPtr& body)
{
    try
    {
        const auto box = body->CalcBndBox(true);
        if (box.IsEmpty())
        {
            return 100.0;
        }
        const double dx = box.MaxPoint().X() - box.MinPoint().X();
        const double dy = box.MaxPoint().Y() - box.MinPoint().Y();
        const double dz = box.MaxPoint().Z() - box.MinPoint().Z();
        const double diagonal = std::sqrt(dx * dx + dy * dy + dz * dz);
        return diagonal > 1e-9 ? diagonal : 100.0;
    }
    catch (...)
    {
        return 100.0;
    }
}

FaceMesh TessellateFace(const sggk::FacePtr& face, double modelSize)
{
    FaceMesh mesh;
    auto surface = face->GeomSurface();
    if (!surface)
    {
        return mesh;
    }
    // Fine level plus a tighter absolute deflection: the default MediumFine
    // grid produced visibly planar artifacts on curved surfaces.
    sggk::SrfMeshOpt opts(sggk::SrfLevel::Fine, modelSize);
    opts.deflection = modelSize * 0.0002;
    auto surfaceMesh = sggk::SurfaceMeshUtil::Tessellate(*surface, face->CalcUVBound(), opts);
    mesh.verts.reserve(surfaceMesh.points.size() * 3);
    for (const auto& point : surfaceMesh.points)
    {
        mesh.verts.push_back(point.X());
        mesh.verts.push_back(point.Y());
        mesh.verts.push_back(point.Z());
    }
    if (surfaceMesh.meshType == sggk::GridMode::Triangle)
    {
        mesh.tris = surfaceMesh.indices;
    }
    else
    {
        // Rectangle grids are split into two triangles per quad.
        const auto& indices = surfaceMesh.indices;
        mesh.tris.reserve(indices.size() / 4 * 6);
        for (size_t i = 0; i + 3 < indices.size(); i += 4)
        {
            mesh.tris.push_back(indices[i]);
            mesh.tris.push_back(indices[i + 1]);
            mesh.tris.push_back(indices[i + 2]);
            mesh.tris.push_back(indices[i]);
            mesh.tris.push_back(indices[i + 2]);
            mesh.tris.push_back(indices[i + 3]);
        }
    }
    return mesh;
}

void Decimate(std::vector<BodyMesh>& bodies, size_t maxTriangles)
{
    size_t total = 0;
    for (const auto& body : bodies)
    {
        total += body.triangleCount;
    }
    if (total <= maxTriangles)
    {
        return;
    }
    size_t kept = 0;
    for (auto& body : bodies)
    {
        const size_t budget = body.triangleCount * maxTriangles / total;
        std::vector<FaceMesh> trimmed;
        size_t bodyKept = 0;
        for (auto& face : body.faces)
        {
            const size_t faceTris = face.tris.size() / 3;
            if (bodyKept + faceTris <= budget)
            {
                bodyKept += faceTris;
                trimmed.push_back(std::move(face));
                continue;
            }
            const size_t remaining = budget > bodyKept ? budget - bodyKept : 0;
            if (remaining >= 3)
            {
                const size_t stride = faceTris / remaining + 1;
                FaceMesh decimated;
                decimated.verts = face.verts;
                for (size_t t = 0; t < faceTris && decimated.tris.size() / 3 < remaining; t += stride)
                {
                    decimated.tris.push_back(face.tris[t * 3]);
                    decimated.tris.push_back(face.tris[t * 3 + 1]);
                    decimated.tris.push_back(face.tris[t * 3 + 2]);
                }
                bodyKept += decimated.tris.size() / 3;
                trimmed.push_back(std::move(decimated));
            }
        }
        body.faces = std::move(trimmed);
        body.triangleCount = bodyKept;
        kept += bodyKept;
    }
}

void WriteJson(const fs::path& path, const std::vector<BodyMesh>& bodies, size_t totalTriangles)
{
    std::ostringstream os;
    os << std::setprecision(9);
    os << "{\n  \"schema_version\": 1,\n  \"kind\": \"sggk_mesh_dump\",\n"
       << "  \"triangle_count\": " << totalTriangles << ",\n  \"bodies\": [\n";
    for (size_t b = 0; b < bodies.size(); ++b)
    {
        const auto& body = bodies[b];
        os << (b ? ",\n" : "") << "    {\"name\": \"" << EscapeJson(body.name)
           << "\", \"triangles\": " << body.triangleCount << ", \"faces\": [\n";
        for (size_t f = 0; f < body.faces.size(); ++f)
        {
            const auto& face = body.faces[f];
            os << (f ? ",\n" : "") << "      {\"verts\": [";
            for (size_t i = 0; i < face.verts.size(); ++i)
            {
                os << (i ? "," : "") << face.verts[i];
            }
            os << "], \"tris\": [";
            for (size_t i = 0; i < face.tris.size(); ++i)
            {
                os << (i ? "," : "") << face.tris[i];
            }
            os << "]}";
        }
        os << "\n    ]}";
    }
    os << "\n  ]\n}\n";
    std::ofstream file(path, std::ios::binary);
    file << os.str();
}

int Run(const Options& opts)
{
    std::vector<BodyMesh> bodies;
    for (const auto& [name, source] : opts.bodies)
    {
        if (!fs::is_regular_file(source))
        {
            throw std::runtime_error("SGT not found: " + source.string());
        }
        auto loaded = LoadBodies(source);
        for (size_t index = 0; index < loaded.size(); ++index)
        {
            const auto& body = loaded[index];
            if (!body)
            {
                continue;
            }
            BodyMesh entry;
            entry.name = loaded.size() > 1
                ? SanitizeName(name) + "#" + std::to_string(index)
                : SanitizeName(name);
            const double modelSize = BodyDiagonal(body);
            for (const auto& face : body->QueryFaces())
            {
                if (!face)
                {
                    continue;
                }
                try
                {
                    FaceMesh mesh = TessellateFace(face, modelSize);
                    entry.triangleCount += mesh.tris.size() / 3;
                    if (!mesh.tris.empty())
                    {
                        entry.faces.push_back(std::move(mesh));
                    }
                }
                catch (const std::exception&)
                {
                    // A single non-tessellatable face must not fail the dump.
                }
            }
            bodies.push_back(std::move(entry));
        }
    }
    Decimate(bodies, kMaxTriangles);
    size_t total = 0;
    for (const auto& body : bodies)
    {
        total += body.triangleCount;
    }
    fs::create_directories(opts.outDir);
    const fs::path outPath = opts.outDir / "mesh.json";
    WriteJson(outPath, bodies, total);
    std::cout << "bodies=" << bodies.size() << "\n"
              << "triangles=" << total << "\n"
              << "out=" << fs::absolute(outPath).string() << "\n";
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
        std::cerr << "sggk_mesh_dump: " << ex.what() << "\n";
        return 1;
    }
}
