#include <Boolean/API.h>
#include <GeomBase/Axis1.h>
#include <GeomBase/BndBox.h>
#include <Foundation/SGGK_Version.h>
#include <Foundation/init.h>
#include <GeomAlgo/Offset2D/Offset2D.h>
#include <GeomBase/BndBox2D.h>
#include <GeomBase/Matrix4.h>
#include <GeomBase/Point2D.h>
#include <Geometry/2D/Circle2D.h>
#include <Geometry/2D/Line2D.h>
#include <Geometry/2D/TrimmedCurve2D.h>
#include <Geometry/3D/Curve/BoundedCurve3D.h>
#include <Geometry/3D/Curve/Circle3D.h>
#include <Geometry/3D/Surface/BSplineSurface.h>
#include <Geometry/3D/Surface/Surface.h>
#include <IgesExchange/API.h>
#include <ModelAnalysis/API.h>
#include <ModelingBase/API.h>
#include <ModelingPrim/API.h>
#include <Offset/API.h>
#include <StepExchange/API.h>
#include <Topology/Brep/Body.h>
#include <Topology/Brep/Edge.h>
#include <Topology/Brep/Face.h>
#include <Topology/Brep/Topology.h>
#include <Topology/Brep/Vertex.h>
#include <Topology/Serialize/RapidTopoJsonDeserializer.h>
#include <Topology/Serialize/RapidTopoJsonSerializer.h>
#include <Topology/Tools/PtFaceRelation.h>
#include <Topology/Tools/PtBodyRelation.h>
#include <Topology/Tools/TopoBuilder.h>
#include <Topology/Tools/TopoCheckTool.h>
#include <Topology/Tools/TopoPropertyTool.h>

#include "generated_plugin_headers.inc"

#include <algorithm>
#include <array>
#include <cctype>
#include <chrono>
#include <cstring>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <list>
#include <map>
#include <regex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace fs = std::filesystem;

namespace
{
constexpr double kDefaultMaxModelSize = 5e5;
using KeyPointMap = std::map<std::string, std::array<double, 3>>;

struct BodySpec
{
    std::string kind;
    std::string booleanType = "SUBTRACTION";
    double radius = 100.0;
    double height = 100.0;
    double angle = sggk::PI2;
    bool createSeamEdge = true;
    double length = 100.0;
    double width = 100.0;
    double bottomRadius = 100.0;
    double topRadius = 50.0;
    double innerRadius = 50.0;
    double outerRadius = 100.0;
    double longRadius = 150.0;
    double shortRadius = 30.0;
    double profileRadius = 25.0;
    double pathRadius = 150.0;
    double secondaryHeight = 150.0;
    double secondaryTranslateX = 0.0;
    double secondaryTranslateY = 0.0;
    double secondaryTranslateZ = 0.0;
    double minDist = -10.0;
    double maxDist = 20.0;
    double operationTol = sggk::Precision::DefModelingTol;
    double g1Tol = 0.1;
    bool allowPartialSuccess = true;
    double translateX = 0.0;
    double translateY = 0.0;
    double translateZ = 0.0;
    double scale = 1.0;
    fs::path sourceFile;
    int bodyIndex = 0;
    std::vector<std::string> operations;
};

struct BooleanRecipe
{
    BodySpec target;
    BodySpec tool;

    BooleanRecipe()
    {
        target.kind = "solid_cylinder";
        target.radius = 200.0;
        target.height = 500.0;
        target.angle = sggk::PI2 / 2.0;
        target.createSeamEdge = true;

        tool.kind = "solid_wedge";
        tool.length = 100.0;
        tool.width = 200.0;
        tool.height = 150.0;
    }
};

struct Offset2DSegmentSpec
{
    std::string kind = "line";
    bool sense = true;
    double x1 = 0.0;
    double y1 = 0.0;
    double x2 = 100.0;
    double y2 = 0.0;
    double centerX = 0.0;
    double centerY = 0.0;
    double radius = 10.0;
    double startAngle = 0.0;
    double endAngle = sggk::PI2;
    bool ccw = true;
};

struct Offset2DRecipe
{
    double distance = 1.0;
    std::vector<double> distances;
    std::vector<Offset2DSegmentSpec> path;
    double distTol = sggk::Precision::DefModelingTol;
    double angleTol = sggk::Toler::DefAngleTol();
    std::string connectType = "ByLineSeg";
    bool allowCrvDegenerated = true;
    bool allowCrvReversed = true;
    bool allowSelfIntersections = false;
    std::string extendType = "NatruralExtend";
    std::string expectedStatus = "Success";
    bool resultPathCountMinSet = false;
    int resultPathCountMin = 0;
    bool resultPathCountMaxSet = false;
    int resultPathCountMax = 0;
};

struct NumericExpectation
{
    bool minSet = false;
    double minValue = 0.0;
    bool maxSet = false;
    double maxValue = 0.0;
    bool expectedSet = false;
    double expectedValue = 0.0;
    double absTol = 1e-7;
    double relTol = 1e-8;
};

struct PointRelationExpectation
{
    std::string id;
    std::string role = "result";
    int bodyIndex = 0;
    std::string pointRef;
    double x = 0.0;
    double y = 0.0;
    double z = 0.0;
    std::string expected = "Inside";
    double tolerance = -1.0;
    bool checkBoundary = true;
    bool required = true;
};

struct FacePointRelationExpectation
{
    std::string id;
    std::string role = "result";
    int bodyIndex = 0;
    int faceIndex = 0;
    sggk::ID faceId = 0;
    bool useFaceId = false;
    bool hasPoint = false;
    std::string pointRef;
    double x = 0.0;
    double y = 0.0;
    double z = 0.0;
    bool hasUv = false;
    double u = 0.0;
    double v = 0.0;
    bool hasUvFraction = true;
    double uFraction = 0.5;
    double vFraction = 0.5;
    std::string expected = "Inside";
    double tolerance = -1.0;
    bool checkBoundary = true;
    bool required = true;
};

struct ClashExpectation
{
    std::string id;
    std::string roleA = "target";
    std::string roleB = "tool";
    int bodyIndexA = 0;
    int bodyIndexB = 0;
    std::string expected = "Clash_None";
    std::string mode = "ClashClassify";
    double tolerance = -1.0;
    bool required = true;
};

struct DistanceExpectation
{
    std::string id;
    std::string roleA = "target";
    std::string roleB = "tool";
    int bodyIndexA = 0;
    int bodyIndexB = 0;
    std::string kind = "minimum";
    double threshold = -1.0;
    NumericExpectation distance;
    bool required = true;
};

struct PlaneExtremeExpectation
{
    std::string id;
    std::string role = "result";
    int bodyIndex = 0;
    std::string axis = "x";
    std::string side = "min";
    bool expectedSet = false;
    double expected = 0.0;
    bool compareExpected = true;
    double tolerance = sggk::Precision::DefModelingTol;
    bool probeCoordinateSet = false;
    double probeCoordinate = 0.0;
    double planeSpan = 0.0;
    double planeSpanScale = 4.0;
    bool required = true;
    bool exportDebugGeometry = true;
};

struct ValidationExpectations
{
    int minResultBodies = 1;
    bool maxResultBodiesSet = false;
    int maxResultBodies = 0;
    bool requirePropertyCalculations = true;
    bool requireFiniteProperties = true;
    bool requireNonnegativeLengthArea = true;
    bool requireNonnegativeVolume = false;
    bool booleanVolumeRelation = true;
    bool booleanBboxRelation = false;
    bool sampleInputProperties = false;
    double relationAbsTol = 1e-7;
    double relationRelTol = 1e-8;
    NumericExpectation totalLength;
    NumericExpectation totalArea;
    NumericExpectation totalVolume;
    NumericExpectation totalAbsVolume;
    std::vector<PointRelationExpectation> pointRelations;
    std::vector<FacePointRelationExpectation> facePointRelations;
    std::vector<ClashExpectation> clashChecks;
    std::vector<DistanceExpectation> distanceChecks;
    std::vector<PlaneExtremeExpectation> planeExtremeChecks;
};

struct CountExpectation
{
    bool minSet = false;
    int min = 0;
    bool maxSet = false;
    int max = 0;
};

struct SplitRecipe
{
    bool targetAddFace = false;
    bool strictSplit = false;
    bool mergeImprint = false;
    CountExpectation outerBodies;
    CountExpectation innerBodies;
    CountExpectation wireBodies;
    CountExpectation totalBodies;
};

struct SliceRecipe
{
    CountExpectation resultBodies;
    CountExpectation wireBodies;
};

struct TopologySectionRecipe
{
    CountExpectation edges;
    CountExpectation vertices;
    CountExpectation total;
};

struct CaseRecipe
{
    std::string caseId = "boolean_smoke";
    std::string api = "api_boolean";
    std::string booleanType = "SUBTRACTION";
    double modelingTol = sggk::Precision::DefModelingTol;
    double offsetDistance = 0.05;
    double maxModelSize = kDefaultMaxModelSize;
    bool checkValid = true;
    bool topoTrack = true;
    bool nonDestructive = true;

    fs::path sourceFile;
    int sourceBodyIndex = 0;
    BodySpec offsetSource;
    std::string stepAppProtocol = "AP203";
    bool stepSurfaceToBSpline = false;
    bool stepCurveToBSpline = false;
    bool stepSpcurveInWireToBSpline = false;
    bool igesFaceOnlyMode = false;
    bool igesWriteSGKSpecifiedData = false;
    double roundtripAbsTol = sggk::Precision::DefModelingTol;
    double roundtripRelTol = 1e-5;
    BooleanRecipe boolean;
    SplitRecipe split;
    SliceRecipe slice;
    TopologySectionRecipe topologySection;
    Offset2DRecipe offset2d;
    std::string dslSource;
    std::string dslCaseId;
    std::string dslVariant;
    std::string hypothesis;
    std::string sourceRef;
    std::string sourceTaskId;
    std::string sourceTaskPath;
    std::string sourceRiskId;
    std::string sourceRiskFamily;
    std::string sourceRiskCategories;
    ValidationExpectations expectations;
};

struct BodyProperties
{
    int index = 0;
    sggk::ID bodyId = 0;
    std::string summaryJson;
    std::string bboxJson;
    bool bboxOk = false;
    double minX = 0.0;
    double minY = 0.0;
    double minZ = 0.0;
    double maxX = 0.0;
    double maxY = 0.0;
    double maxZ = 0.0;
    double length = 0.0;
    double area = 0.0;
    double volume = 0.0;
    bool propertyOk = false;
    std::string propertyError;
};

struct CliOptions
{
    fs::path recipePath;
    fs::path outRoot = "artifacts";
    std::string caseIdOverride;
    int sdkThreads = 1;
    bool listAdaptersJson = false;
    bool captureFlatTopoTrack = false;
};

class SggkSession
{
public:
    explicit SggkSession(int threadCount)
    {
        sggk::init(nullptr, threadCount);
        m_initialized = true;
    }

    SggkSession(const SggkSession&) = delete;
    SggkSession& operator=(const SggkSession&) = delete;

    ~SggkSession()
    {
        if (m_initialized)
        {
            sggk::fini();
        }
    }

private:
    bool m_initialized = false;
};

struct TopologyRef
{
    std::string role;
    std::string type;
    sggk::ID id = 0;
    int localIndex = 0;
    sggk::ID bodyId = 0;
    std::vector<std::string> operations;
    sggk::TopologyPtr topology;
};

struct InputTopologyIndex
{
    std::vector<TopologyRef> entries;
    std::map<const sggk::Topology*, size_t> byPtr;
    std::map<std::string, std::vector<size_t>> byRoleTypeId;
    std::map<std::string, std::vector<size_t>> byTypeId;
    std::map<const sggk::Body*, std::string> roleByBodyPtr;
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
        case '\b': os << "\\b"; break;
        case '\f': os << "\\f"; break;
        case '\n': os << "\\n"; break;
        case '\r': os << "\\r"; break;
        case '\t': os << "\\t"; break;
        default:
            if (static_cast<unsigned char>(ch) < 0x20)
            {
                os << "\\u" << std::hex << std::setw(4) << std::setfill('0')
                   << static_cast<int>(static_cast<unsigned char>(ch));
            }
            else
            {
                os << ch;
            }
        }
    }
    return os.str();
}

std::string NowIsoLike()
{
    const auto now = std::chrono::system_clock::now();
    const auto time = std::chrono::system_clock::to_time_t(now);
    std::tm tm {};
#ifdef _WIN32
    localtime_s(&tm, &time);
#else
    localtime_r(&time, &tm);
#endif
    std::ostringstream os;
    os << std::put_time(&tm, "%Y-%m-%dT%H:%M:%S");
    return os.str();
}

void WriteTextFile(const fs::path& path, const std::string& text)
{
    fs::create_directories(path.parent_path());
    std::ofstream out(path, std::ios::binary);
    if (!out)
    {
        throw std::runtime_error("failed to open file for writing: " + path.string());
    }
    out << text;
}

std::string ReadTextFile(const fs::path& path)
{
    std::ifstream in(path, std::ios::binary);
    if (!in)
    {
        throw std::runtime_error("failed to open recipe: " + path.string());
    }
    std::ostringstream buffer;
    buffer << in.rdbuf();
    return buffer.str();
}

struct JsonValue
{
    enum class Type
    {
        Null,
        Bool,
        Number,
        String,
        Array,
        Object
    };

    Type type = Type::Null;
    bool boolValue = false;
    double numberValue = 0.0;
    std::string stringValue;
    std::vector<JsonValue> arrayValue;
    std::map<std::string, JsonValue> objectValue;

    bool IsNull() const { return type == Type::Null; }
    bool IsBool() const { return type == Type::Bool; }
    bool IsNumber() const { return type == Type::Number; }
    bool IsString() const { return type == Type::String; }
    bool IsArray() const { return type == Type::Array; }
    bool IsObject() const { return type == Type::Object; }
};

class JsonParser
{
public:
    explicit JsonParser(const std::string& text) : m_text(text) {}

    JsonValue Parse()
    {
        auto value = ParseValue();
        SkipWs();
        if (m_pos != m_text.size())
        {
            throw std::runtime_error("unexpected trailing JSON at offset " + std::to_string(m_pos));
        }
        return value;
    }

private:
    void SkipWs()
    {
        while (m_pos < m_text.size() && std::isspace(static_cast<unsigned char>(m_text[m_pos])))
        {
            ++m_pos;
        }
    }

    char Peek()
    {
        SkipWs();
        if (m_pos >= m_text.size())
        {
            throw std::runtime_error("unexpected end of JSON");
        }
        return m_text[m_pos];
    }

    bool Consume(char ch)
    {
        SkipWs();
        if (m_pos < m_text.size() && m_text[m_pos] == ch)
        {
            ++m_pos;
            return true;
        }
        return false;
    }

    void Expect(char ch)
    {
        if (!Consume(ch))
        {
            throw std::runtime_error(std::string("expected '") + ch + "' at offset " + std::to_string(m_pos));
        }
    }

    JsonValue ParseValue()
    {
        const char ch = Peek();
        if (ch == '{')
        {
            return ParseObject();
        }
        if (ch == '[')
        {
            return ParseArray();
        }
        if (ch == '"')
        {
            JsonValue value;
            value.type = JsonValue::Type::String;
            value.stringValue = ParseString();
            return value;
        }
        if (ch == '-' || std::isdigit(static_cast<unsigned char>(ch)))
        {
            return ParseNumber();
        }
        if (MatchLiteral("true"))
        {
            JsonValue value;
            value.type = JsonValue::Type::Bool;
            value.boolValue = true;
            return value;
        }
        if (MatchLiteral("false"))
        {
            JsonValue value;
            value.type = JsonValue::Type::Bool;
            value.boolValue = false;
            return value;
        }
        if (MatchLiteral("null"))
        {
            return JsonValue();
        }
        throw std::runtime_error("unexpected JSON value at offset " + std::to_string(m_pos));
    }

    bool MatchLiteral(const char* literal)
    {
        SkipWs();
        const size_t len = std::strlen(literal);
        if (m_text.compare(m_pos, len, literal) == 0)
        {
            m_pos += len;
            return true;
        }
        return false;
    }

    JsonValue ParseObject()
    {
        JsonValue value;
        value.type = JsonValue::Type::Object;
        Expect('{');
        if (Consume('}'))
        {
            return value;
        }
        while (true)
        {
            if (Peek() != '"')
            {
                throw std::runtime_error("expected object key at offset " + std::to_string(m_pos));
            }
            const std::string key = ParseString();
            Expect(':');
            value.objectValue[key] = ParseValue();
            if (Consume('}'))
            {
                return value;
            }
            Expect(',');
        }
    }

    JsonValue ParseArray()
    {
        JsonValue value;
        value.type = JsonValue::Type::Array;
        Expect('[');
        if (Consume(']'))
        {
            return value;
        }
        while (true)
        {
            value.arrayValue.push_back(ParseValue());
            if (Consume(']'))
            {
                return value;
            }
            Expect(',');
        }
    }

    std::string ParseString()
    {
        Expect('"');
        std::string result;
        while (m_pos < m_text.size())
        {
            const char ch = m_text[m_pos++];
            if (ch == '"')
            {
                return result;
            }
            if (ch != '\\')
            {
                result.push_back(ch);
                continue;
            }
            if (m_pos >= m_text.size())
            {
                throw std::runtime_error("unterminated JSON escape");
            }
            const char esc = m_text[m_pos++];
            switch (esc)
            {
            case '"': result.push_back('"'); break;
            case '\\': result.push_back('\\'); break;
            case '/': result.push_back('/'); break;
            case 'b': result.push_back('\b'); break;
            case 'f': result.push_back('\f'); break;
            case 'n': result.push_back('\n'); break;
            case 'r': result.push_back('\r'); break;
            case 't': result.push_back('\t'); break;
            case 'u':
                if (m_pos + 4 > m_text.size())
                {
                    throw std::runtime_error("short JSON unicode escape");
                }
                result.push_back('?');
                m_pos += 4;
                break;
            default:
                throw std::runtime_error("unsupported JSON escape at offset " + std::to_string(m_pos));
            }
        }
        throw std::runtime_error("unterminated JSON string");
    }

    JsonValue ParseNumber()
    {
        SkipWs();
        const size_t start = m_pos;
        if (m_text[m_pos] == '-')
        {
            ++m_pos;
        }
        while (m_pos < m_text.size() && std::isdigit(static_cast<unsigned char>(m_text[m_pos])))
        {
            ++m_pos;
        }
        if (m_pos < m_text.size() && m_text[m_pos] == '.')
        {
            ++m_pos;
            while (m_pos < m_text.size() && std::isdigit(static_cast<unsigned char>(m_text[m_pos])))
            {
                ++m_pos;
            }
        }
        if (m_pos < m_text.size() && (m_text[m_pos] == 'e' || m_text[m_pos] == 'E'))
        {
            ++m_pos;
            if (m_pos < m_text.size() && (m_text[m_pos] == '+' || m_text[m_pos] == '-'))
            {
                ++m_pos;
            }
            while (m_pos < m_text.size() && std::isdigit(static_cast<unsigned char>(m_text[m_pos])))
            {
                ++m_pos;
            }
        }

        JsonValue value;
        value.type = JsonValue::Type::Number;
        value.numberValue = std::stod(m_text.substr(start, m_pos - start));
        return value;
    }

    const std::string& m_text;
    size_t m_pos = 0;
};

const JsonValue* JsonFind(const JsonValue& object, const std::string& key)
{
    if (!object.IsObject())
    {
        return nullptr;
    }
    const auto it = object.objectValue.find(key);
    return it == object.objectValue.end() ? nullptr : &it->second;
}

JsonValue* JsonFind(JsonValue& object, const std::string& key)
{
    if (!object.IsObject())
    {
        return nullptr;
    }
    const auto it = object.objectValue.find(key);
    return it == object.objectValue.end() ? nullptr : &it->second;
}

std::string JsonString(const JsonValue& value, const std::string& label)
{
    if (!value.IsString())
    {
        throw std::runtime_error(label + " must be a string");
    }
    return value.stringValue;
}

bool JsonBool(const JsonValue& value, const std::string& label)
{
    if (!value.IsBool())
    {
        throw std::runtime_error(label + " must be a bool");
    }
    return value.boolValue;
}

class ExprParser
{
public:
    ExprParser(const std::string& text, const std::map<std::string, double>& symbols)
        : m_text(text), m_symbols(symbols)
    {
    }

    double Parse()
    {
        const double result = ParseAddSub();
        SkipWs();
        if (m_pos != m_text.size())
        {
            throw std::runtime_error("unexpected expression token in: " + m_text);
        }
        return result;
    }

private:
    void SkipWs()
    {
        while (m_pos < m_text.size() && std::isspace(static_cast<unsigned char>(m_text[m_pos])))
        {
            ++m_pos;
        }
    }

    bool Consume(char ch)
    {
        SkipWs();
        if (m_pos < m_text.size() && m_text[m_pos] == ch)
        {
            ++m_pos;
            return true;
        }
        return false;
    }

    double ParseAddSub()
    {
        double value = ParseMulDiv();
        while (true)
        {
            if (Consume('+'))
            {
                value += ParseMulDiv();
            }
            else if (Consume('-'))
            {
                value -= ParseMulDiv();
            }
            else
            {
                return value;
            }
        }
    }

    double ParseMulDiv()
    {
        double value = ParsePower();
        while (true)
        {
            if (Consume('*'))
            {
                if (Consume('*'))
                {
                    m_pos -= 2;
                    return value;
                }
                value *= ParsePower();
            }
            else if (Consume('/'))
            {
                value /= ParsePower();
            }
            else
            {
                return value;
            }
        }
    }

    double ParsePower()
    {
        double value = ParseUnary();
        SkipWs();
        if (m_pos + 1 < m_text.size() && m_text[m_pos] == '*' && m_text[m_pos + 1] == '*')
        {
            m_pos += 2;
            value = std::pow(value, ParsePower());
        }
        return value;
    }

    double ParseUnary()
    {
        if (Consume('+'))
        {
            return ParseUnary();
        }
        if (Consume('-'))
        {
            return -ParseUnary();
        }
        return ParsePrimary();
    }

    double ParsePrimary()
    {
        SkipWs();
        if (Consume('('))
        {
            const double value = ParseAddSub();
            if (!Consume(')'))
            {
                throw std::runtime_error("missing ')' in expression: " + m_text);
            }
            return value;
        }
        if (m_pos < m_text.size() && (std::isdigit(static_cast<unsigned char>(m_text[m_pos])) || m_text[m_pos] == '.'))
        {
            const size_t start = m_pos;
            while (m_pos < m_text.size() &&
                   (std::isdigit(static_cast<unsigned char>(m_text[m_pos])) || m_text[m_pos] == '.' ||
                    m_text[m_pos] == 'e' || m_text[m_pos] == 'E' || m_text[m_pos] == '+' || m_text[m_pos] == '-'))
            {
                if ((m_text[m_pos] == '+' || m_text[m_pos] == '-') && m_pos > start &&
                    m_text[m_pos - 1] != 'e' && m_text[m_pos - 1] != 'E')
                {
                    break;
                }
                ++m_pos;
            }
            return std::stod(m_text.substr(start, m_pos - start));
        }
        if (m_pos < m_text.size() && (std::isalpha(static_cast<unsigned char>(m_text[m_pos])) || m_text[m_pos] == '_'))
        {
            const size_t start = m_pos;
            while (m_pos < m_text.size() &&
                   (std::isalnum(static_cast<unsigned char>(m_text[m_pos])) || m_text[m_pos] == '_'))
            {
                ++m_pos;
            }
            const std::string name = m_text.substr(start, m_pos - start);
            const auto it = m_symbols.find(name);
            if (it == m_symbols.end())
            {
                throw std::runtime_error("unknown expression symbol: " + name);
            }
            return it->second;
        }
        throw std::runtime_error("expected expression primary in: " + m_text);
    }

    const std::string& m_text;
    const std::map<std::string, double>& m_symbols;
    size_t m_pos = 0;
};

double JsonNumber(const JsonValue& value, const std::map<std::string, double>& symbols, const std::string& label)
{
    if (value.IsNumber())
    {
        return value.numberValue;
    }
    if (value.IsString())
    {
        return ExprParser(value.stringValue, symbols).Parse();
    }
    throw std::runtime_error(label + " must be a number or numeric expression");
}

int JsonInteger(const JsonValue& value, const std::map<std::string, double>& symbols, const std::string& label)
{
    const double raw = JsonNumber(value, symbols, label);
    const int rounded = static_cast<int>(std::llround(raw));
    if (std::fabs(raw - static_cast<double>(rounded)) > 1e-9)
    {
        throw std::runtime_error(label + " must be an integer");
    }
    return rounded;
}

std::array<double, 3> JsonPoint3Array(
    const JsonValue& value,
    const std::map<std::string, double>& symbols,
    const std::string& label)
{
    if (!value.IsArray() || value.arrayValue.size() != 3)
    {
        throw std::runtime_error(label + " must be a three-number array");
    }
    return {
        JsonNumber(value.arrayValue[0], symbols, label + ".0"),
        JsonNumber(value.arrayValue[1], symbols, label + ".1"),
        JsonNumber(value.arrayValue[2], symbols, label + ".2"),
    };
}

std::array<double, 2> JsonPoint2Array(
    const JsonValue& value,
    const std::map<std::string, double>& symbols,
    const std::string& label)
{
    if (!value.IsArray() || value.arrayValue.size() != 2)
    {
        throw std::runtime_error(label + " must be a two-number array");
    }
    return {
        JsonNumber(value.arrayValue[0], symbols, label + ".0"),
        JsonNumber(value.arrayValue[1], symbols, label + ".1"),
    };
}

void AddDslKeyPoints(
    KeyPointMap& points,
    const JsonValue& value,
    const std::map<std::string, double>& symbols,
    const std::string& label)
{
    if (!value.IsObject())
    {
        throw std::runtime_error(label + " must be an object");
    }
    for (const auto& item : value.objectValue)
    {
        if (item.first.empty())
        {
            throw std::runtime_error(label + " names must be non-empty");
        }
        const JsonValue* pointValue = &item.second;
        if (item.second.IsObject())
        {
            pointValue = JsonFind(item.second, "point");
            if (!pointValue)
            {
                throw std::runtime_error(label + "." + item.first + ".point is required");
            }
        }
        points[item.first] = JsonPoint3Array(*pointValue, symbols, label + "." + item.first);
    }
}

KeyPointMap DslKeyPointsFromContainer(
    const JsonValue& object,
    const std::map<std::string, double>& symbols,
    const std::string& label)
{
    KeyPointMap points;
    if (const auto keyPoints = JsonFind(object, "key_points"))
    {
        AddDslKeyPoints(points, *keyPoints, symbols, label + ".key_points");
    }
    return points;
}

void ApplyPointRef(
    const JsonValue& item,
    const KeyPointMap& keyPoints,
    const std::string& itemLabel,
    std::string& pointRef,
    double& x,
    double& y,
    double& z,
    bool* hasPoint = nullptr)
{
    const auto ref = JsonFind(item, "point_ref");
    if (!ref)
    {
        return;
    }
    pointRef = JsonString(*ref, itemLabel + ".point_ref");
    if (JsonFind(item, "point"))
    {
        return;
    }
    const auto it = keyPoints.find(pointRef);
    if (it == keyPoints.end())
    {
        throw std::runtime_error(itemLabel + ".point_ref is not defined in key_points: " + pointRef);
    }
    x = it->second[0];
    y = it->second[1];
    z = it->second[2];
    if (hasPoint)
    {
        *hasPoint = true;
    }
}

void ApplyNumericExpectation(
    NumericExpectation& expectation,
    const JsonValue& object,
    const std::map<std::string, double>& symbols,
    const std::string& label)
{
    if (!object.IsObject())
    {
        throw std::runtime_error(label + " must be an object");
    }
    if (const auto value = JsonFind(object, "min"))
    {
        expectation.minSet = true;
        expectation.minValue = JsonNumber(*value, symbols, label + ".min");
    }
    if (const auto value = JsonFind(object, "max"))
    {
        expectation.maxSet = true;
        expectation.maxValue = JsonNumber(*value, symbols, label + ".max");
    }
    if (const auto value = JsonFind(object, "expected"))
    {
        expectation.expectedSet = true;
        expectation.expectedValue = JsonNumber(*value, symbols, label + ".expected");
    }
    if (const auto value = JsonFind(object, "abs_tol"))
    {
        expectation.absTol = JsonNumber(*value, symbols, label + ".abs_tol");
    }
    if (const auto value = JsonFind(object, "rel_tol"))
    {
        expectation.relTol = JsonNumber(*value, symbols, label + ".rel_tol");
    }
}

void ApplyMetricShorthand(
    const JsonValue& object,
    const std::map<std::string, double>& symbols,
    const std::string& prefix,
    NumericExpectation& metric)
{
    if (const auto value = JsonFind(object, "min_" + prefix))
    {
        metric.minSet = true;
        metric.minValue = JsonNumber(*value, symbols, "min_" + prefix);
    }
    if (const auto value = JsonFind(object, "max_" + prefix))
    {
        metric.maxSet = true;
        metric.maxValue = JsonNumber(*value, symbols, "max_" + prefix);
    }
    if (const auto value = JsonFind(object, "expected_" + prefix))
    {
        metric.expectedSet = true;
        metric.expectedValue = JsonNumber(*value, symbols, "expected_" + prefix);
    }
    if (const auto value = JsonFind(object, prefix + "_abs_tol"))
    {
        metric.absTol = JsonNumber(*value, symbols, prefix + "_abs_tol");
    }
    if (const auto value = JsonFind(object, prefix + "_rel_tol"))
    {
        metric.relTol = JsonNumber(*value, symbols, prefix + "_rel_tol");
    }
}

std::vector<PointRelationExpectation> ParsePointRelations(
    const JsonValue& value,
    const std::map<std::string, double>& symbols,
    const KeyPointMap& keyPoints,
    const std::string& label,
    double defaultTolerance)
{
    if (!value.IsArray())
    {
        throw std::runtime_error(label + " must be an array");
    }
    std::vector<PointRelationExpectation> relations;
    for (size_t index = 0; index < value.arrayValue.size(); ++index)
    {
        const auto& item = value.arrayValue[index];
        const std::string itemLabel = label + "." + std::to_string(index);
        if (!item.IsObject())
        {
            throw std::runtime_error(itemLabel + " must be an object");
        }
        PointRelationExpectation relation;
        relation.id = "point_relation_" + std::to_string(index);
        relation.tolerance = defaultTolerance;
        if (const auto field = JsonFind(item, "id"))
        {
            relation.id = JsonString(*field, itemLabel + ".id");
        }
        if (const auto field = JsonFind(item, "role"))
        {
            relation.role = JsonString(*field, itemLabel + ".role");
        }
        if (const auto field = JsonFind(item, "body_index"))
        {
            relation.bodyIndex = JsonInteger(*field, symbols, itemLabel + ".body_index");
        }
        if (const auto field = JsonFind(item, "expected"))
        {
            relation.expected = JsonString(*field, itemLabel + ".expected");
        }
        if (const auto field = JsonFind(item, "tolerance"))
        {
            relation.tolerance = JsonNumber(*field, symbols, itemLabel + ".tolerance");
        }
        if (const auto field = JsonFind(item, "check_boundary"))
        {
            relation.checkBoundary = JsonBool(*field, itemLabel + ".check_boundary");
        }
        if (const auto field = JsonFind(item, "required"))
        {
            relation.required = JsonBool(*field, itemLabel + ".required");
        }
        ApplyPointRef(item, keyPoints, itemLabel, relation.pointRef, relation.x, relation.y, relation.z);
        const auto point = JsonFind(item, "point");
        if (!point && relation.pointRef.empty())
        {
            throw std::runtime_error(itemLabel + ".point must be a three-number array");
        }
        if (point)
        {
            const auto parsedPoint = JsonPoint3Array(*point, symbols, itemLabel + ".point");
            relation.x = parsedPoint[0];
            relation.y = parsedPoint[1];
            relation.z = parsedPoint[2];
        }
        if (relation.bodyIndex < 0)
        {
            throw std::runtime_error(itemLabel + ".body_index must be >= 0");
        }
        if (relation.tolerance <= 0.0)
        {
            throw std::runtime_error(itemLabel + ".tolerance must be > 0");
        }
        relations.push_back(relation);
    }
    return relations;
}

bool IsKnownFacePointExpectedName(const std::string& expected)
{
    return expected == "Unknown" ||
           expected == "OnVertex" ||
           expected == "OnEdge" ||
           expected == "Inside" ||
           expected == "Outside" ||
           expected == "OnBoundary" ||
           expected == "OnFace";
}

std::vector<FacePointRelationExpectation> ParseFacePointRelations(
    const JsonValue& value,
    const std::map<std::string, double>& symbols,
    const KeyPointMap& keyPoints,
    const std::string& label,
    double defaultTolerance)
{
    if (!value.IsArray())
    {
        throw std::runtime_error(label + " must be an array");
    }
    std::vector<FacePointRelationExpectation> relations;
    for (size_t index = 0; index < value.arrayValue.size(); ++index)
    {
        const auto& item = value.arrayValue[index];
        const std::string itemLabel = label + "." + std::to_string(index);
        if (!item.IsObject())
        {
            throw std::runtime_error(itemLabel + " must be an object");
        }
        FacePointRelationExpectation relation;
        relation.id = "face_point_relation_" + std::to_string(index);
        relation.tolerance = defaultTolerance;
        if (const auto field = JsonFind(item, "id"))
        {
            relation.id = JsonString(*field, itemLabel + ".id");
        }
        if (const auto field = JsonFind(item, "role"))
        {
            relation.role = JsonString(*field, itemLabel + ".role");
        }
        if (const auto field = JsonFind(item, "body_index"))
        {
            relation.bodyIndex = JsonInteger(*field, symbols, itemLabel + ".body_index");
        }
        if (const auto field = JsonFind(item, "face_index"))
        {
            relation.faceIndex = JsonInteger(*field, symbols, itemLabel + ".face_index");
        }
        if (const auto field = JsonFind(item, "face_id"))
        {
            relation.faceId = static_cast<sggk::ID>(JsonInteger(*field, symbols, itemLabel + ".face_id"));
            relation.useFaceId = true;
        }
        if (const auto field = JsonFind(item, "expected"))
        {
            relation.expected = JsonString(*field, itemLabel + ".expected");
        }
        if (const auto field = JsonFind(item, "tolerance"))
        {
            relation.tolerance = JsonNumber(*field, symbols, itemLabel + ".tolerance");
        }
        if (const auto field = JsonFind(item, "check_boundary"))
        {
            relation.checkBoundary = JsonBool(*field, itemLabel + ".check_boundary");
        }
        if (const auto field = JsonFind(item, "required"))
        {
            relation.required = JsonBool(*field, itemLabel + ".required");
        }
        ApplyPointRef(item, keyPoints, itemLabel, relation.pointRef, relation.x, relation.y, relation.z, &relation.hasPoint);
        if (const auto point = JsonFind(item, "point"))
        {
            const auto parsedPoint = JsonPoint3Array(*point, symbols, itemLabel + ".point");
            relation.x = parsedPoint[0];
            relation.y = parsedPoint[1];
            relation.z = parsedPoint[2];
            relation.hasPoint = true;
        }
        if (const auto uv = JsonFind(item, "uv"))
        {
            if (!uv->IsArray() || uv->arrayValue.size() != 2)
            {
                throw std::runtime_error(itemLabel + ".uv must be a two-number array");
            }
            relation.u = JsonNumber(uv->arrayValue[0], symbols, itemLabel + ".uv.0");
            relation.v = JsonNumber(uv->arrayValue[1], symbols, itemLabel + ".uv.1");
            relation.hasUv = true;
            relation.hasUvFraction = false;
        }
        if (const auto uvFraction = JsonFind(item, "uv_fraction"))
        {
            if (!uvFraction->IsArray() || uvFraction->arrayValue.size() != 2)
            {
                throw std::runtime_error(itemLabel + ".uv_fraction must be a two-number array");
            }
            relation.uFraction = JsonNumber(uvFraction->arrayValue[0], symbols, itemLabel + ".uv_fraction.0");
            relation.vFraction = JsonNumber(uvFraction->arrayValue[1], symbols, itemLabel + ".uv_fraction.1");
            relation.hasUvFraction = true;
            relation.hasUv = false;
        }
        if (relation.bodyIndex < 0)
        {
            throw std::runtime_error(itemLabel + ".body_index must be >= 0");
        }
        if (relation.faceIndex < 0)
        {
            throw std::runtime_error(itemLabel + ".face_index must be >= 0");
        }
        if (relation.tolerance <= 0.0)
        {
            throw std::runtime_error(itemLabel + ".tolerance must be > 0");
        }
        if (!IsKnownFacePointExpectedName(relation.expected))
        {
            throw std::runtime_error(itemLabel + ".expected is unknown: " + relation.expected);
        }
        relations.push_back(relation);
    }
    return relations;
}

bool IsKnownClashModeName(const std::string& mode)
{
    return mode == "ClashExistenceOnly" ||
           mode == "ClashClassify" ||
           mode == "ClashClassifySubEntities";
}

bool IsKnownClashExpectedName(const std::string& expected)
{
    return expected == "Clash_None" ||
           expected == "Clash_Exists" ||
           expected == "Clash_AInB" ||
           expected == "Clash_BInA" ||
           expected == "Clash_Touch" ||
           expected == "Clash_Interfere" ||
           expected == "NoClash" ||
           expected == "AnyClash";
}

bool IsKnownDistanceKindName(const std::string& kind)
{
    return kind == "minimum" || kind == "maximum";
}

bool IsKnownPlaneAxisName(const std::string& axis)
{
    return axis == "x" || axis == "y" || axis == "z";
}

bool IsKnownPlaneSideName(const std::string& side)
{
    return side == "min" || side == "max";
}

std::vector<ClashExpectation> ParseClashChecks(
    const JsonValue& value,
    const std::map<std::string, double>& symbols,
    const std::string& label,
    double defaultTolerance)
{
    if (!value.IsArray())
    {
        throw std::runtime_error(label + " must be an array");
    }
    std::vector<ClashExpectation> checks;
    for (size_t index = 0; index < value.arrayValue.size(); ++index)
    {
        const auto& item = value.arrayValue[index];
        const std::string itemLabel = label + "." + std::to_string(index);
        if (!item.IsObject())
        {
            throw std::runtime_error(itemLabel + " must be an object");
        }
        ClashExpectation check;
        check.id = "clash_check_" + std::to_string(index);
        check.tolerance = defaultTolerance;
        if (const auto field = JsonFind(item, "id"))
        {
            check.id = JsonString(*field, itemLabel + ".id");
        }
        if (const auto field = JsonFind(item, "role_a"))
        {
            check.roleA = JsonString(*field, itemLabel + ".role_a");
        }
        if (const auto field = JsonFind(item, "role_b"))
        {
            check.roleB = JsonString(*field, itemLabel + ".role_b");
        }
        if (const auto field = JsonFind(item, "body_index_a"))
        {
            check.bodyIndexA = JsonInteger(*field, symbols, itemLabel + ".body_index_a");
        }
        if (const auto field = JsonFind(item, "body_index_b"))
        {
            check.bodyIndexB = JsonInteger(*field, symbols, itemLabel + ".body_index_b");
        }
        if (const auto field = JsonFind(item, "expected"))
        {
            check.expected = JsonString(*field, itemLabel + ".expected");
        }
        if (const auto field = JsonFind(item, "mode"))
        {
            check.mode = JsonString(*field, itemLabel + ".mode");
        }
        if (const auto field = JsonFind(item, "tolerance"))
        {
            check.tolerance = JsonNumber(*field, symbols, itemLabel + ".tolerance");
        }
        if (const auto field = JsonFind(item, "required"))
        {
            check.required = JsonBool(*field, itemLabel + ".required");
        }
        if (check.bodyIndexA < 0)
        {
            throw std::runtime_error(itemLabel + ".body_index_a must be >= 0");
        }
        if (check.bodyIndexB < 0)
        {
            throw std::runtime_error(itemLabel + ".body_index_b must be >= 0");
        }
        if (check.tolerance <= 0.0)
        {
            throw std::runtime_error(itemLabel + ".tolerance must be > 0");
        }
        if (!IsKnownClashModeName(check.mode))
        {
            throw std::runtime_error(itemLabel + ".mode is unknown: " + check.mode);
        }
        if (!IsKnownClashExpectedName(check.expected))
        {
            throw std::runtime_error(itemLabel + ".expected is unknown: " + check.expected);
        }
        checks.push_back(check);
    }
    return checks;
}

std::vector<DistanceExpectation> ParseDistanceChecks(
    const JsonValue& value,
    const std::map<std::string, double>& symbols,
    const std::string& label,
    double defaultTolerance)
{
    if (!value.IsArray())
    {
        throw std::runtime_error(label + " must be an array");
    }
    std::vector<DistanceExpectation> checks;
    for (size_t index = 0; index < value.arrayValue.size(); ++index)
    {
        const auto& item = value.arrayValue[index];
        const std::string itemLabel = label + "." + std::to_string(index);
        if (!item.IsObject())
        {
            throw std::runtime_error(itemLabel + " must be an object");
        }
        DistanceExpectation check;
        check.id = "distance_check_" + std::to_string(index);
        check.distance.absTol = defaultTolerance;
        ApplyNumericExpectation(check.distance, item, symbols, itemLabel);
        if (const auto field = JsonFind(item, "distance"))
        {
            ApplyNumericExpectation(check.distance, *field, symbols, itemLabel + ".distance");
        }
        if (const auto field = JsonFind(item, "id"))
        {
            check.id = JsonString(*field, itemLabel + ".id");
        }
        if (const auto field = JsonFind(item, "role_a"))
        {
            check.roleA = JsonString(*field, itemLabel + ".role_a");
        }
        if (const auto field = JsonFind(item, "role_b"))
        {
            check.roleB = JsonString(*field, itemLabel + ".role_b");
        }
        if (const auto field = JsonFind(item, "body_index_a"))
        {
            check.bodyIndexA = JsonInteger(*field, symbols, itemLabel + ".body_index_a");
        }
        if (const auto field = JsonFind(item, "body_index_b"))
        {
            check.bodyIndexB = JsonInteger(*field, symbols, itemLabel + ".body_index_b");
        }
        if (const auto field = JsonFind(item, "kind"))
        {
            check.kind = JsonString(*field, itemLabel + ".kind");
        }
        if (const auto field = JsonFind(item, "threshold"))
        {
            check.threshold = JsonNumber(*field, symbols, itemLabel + ".threshold");
        }
        if (const auto field = JsonFind(item, "required"))
        {
            check.required = JsonBool(*field, itemLabel + ".required");
        }
        if (check.bodyIndexA < 0)
        {
            throw std::runtime_error(itemLabel + ".body_index_a must be >= 0");
        }
        if (check.bodyIndexB < 0)
        {
            throw std::runtime_error(itemLabel + ".body_index_b must be >= 0");
        }
        if (check.threshold == 0.0)
        {
            throw std::runtime_error(itemLabel + ".threshold must be omitted or > 0");
        }
        if (!IsKnownDistanceKindName(check.kind))
        {
            throw std::runtime_error(itemLabel + ".kind is unknown: " + check.kind);
        }
        checks.push_back(check);
    }
    return checks;
}

std::vector<PlaneExtremeExpectation> ParsePlaneExtremeChecks(
    const JsonValue& value,
    const std::map<std::string, double>& symbols,
    const std::string& label,
    double defaultTolerance)
{
    if (!value.IsArray())
    {
        throw std::runtime_error(label + " must be an array");
    }
    std::vector<PlaneExtremeExpectation> checks;
    for (size_t index = 0; index < value.arrayValue.size(); ++index)
    {
        const auto& item = value.arrayValue[index];
        const std::string itemLabel = label + "." + std::to_string(index);
        if (!item.IsObject())
        {
            throw std::runtime_error(itemLabel + " must be an object");
        }
        PlaneExtremeExpectation check;
        check.id = "plane_extreme_" + std::to_string(index);
        check.tolerance = defaultTolerance;
        if (const auto field = JsonFind(item, "id"))
        {
            check.id = JsonString(*field, itemLabel + ".id");
        }
        if (const auto field = JsonFind(item, "role"))
        {
            check.role = JsonString(*field, itemLabel + ".role");
        }
        if (const auto field = JsonFind(item, "body_index"))
        {
            check.bodyIndex = JsonInteger(*field, symbols, itemLabel + ".body_index");
        }
        if (const auto field = JsonFind(item, "axis"))
        {
            check.axis = JsonString(*field, itemLabel + ".axis");
        }
        if (const auto field = JsonFind(item, "side"))
        {
            check.side = JsonString(*field, itemLabel + ".side");
        }
        if (const auto field = JsonFind(item, "expected"))
        {
            check.expectedSet = true;
            check.expected = JsonNumber(*field, symbols, itemLabel + ".expected");
        }
        if (const auto field = JsonFind(item, "tolerance"))
        {
            check.tolerance = JsonNumber(*field, symbols, itemLabel + ".tolerance");
        }
        if (const auto field = JsonFind(item, "probe_coordinate"))
        {
            check.probeCoordinateSet = true;
            check.probeCoordinate = JsonNumber(*field, symbols, itemLabel + ".probe_coordinate");
        }
        if (const auto field = JsonFind(item, "plane_span"))
        {
            check.planeSpan = JsonNumber(*field, symbols, itemLabel + ".plane_span");
        }
        if (const auto field = JsonFind(item, "plane_span_scale"))
        {
            check.planeSpanScale = JsonNumber(*field, symbols, itemLabel + ".plane_span_scale");
        }
        if (const auto field = JsonFind(item, "required"))
        {
            check.required = JsonBool(*field, itemLabel + ".required");
        }
        if (const auto field = JsonFind(item, "compare_expected"))
        {
            check.compareExpected = JsonBool(*field, itemLabel + ".compare_expected");
        }
        if (const auto field = JsonFind(item, "export_debug_geometry"))
        {
            check.exportDebugGeometry = JsonBool(*field, itemLabel + ".export_debug_geometry");
        }
        if (check.bodyIndex < 0)
        {
            throw std::runtime_error(itemLabel + ".body_index must be >= 0");
        }
        if (!IsKnownPlaneAxisName(check.axis))
        {
            throw std::runtime_error(itemLabel + ".axis must be one of x, y, z");
        }
        if (!IsKnownPlaneSideName(check.side))
        {
            throw std::runtime_error(itemLabel + ".side must be one of min, max");
        }
        if (check.tolerance <= 0.0)
        {
            throw std::runtime_error(itemLabel + ".tolerance must be > 0");
        }
        if (check.planeSpan < 0.0)
        {
            throw std::runtime_error(itemLabel + ".plane_span must be omitted or >= 0");
        }
        if (check.planeSpanScale <= 0.0)
        {
            throw std::runtime_error(itemLabel + ".plane_span_scale must be > 0");
        }
        if (check.compareExpected && !check.expectedSet)
        {
            throw std::runtime_error(itemLabel + ".expected is required when compare_expected is true");
        }
        checks.push_back(check);
    }
    return checks;
}

void ApplyValidationExpectationsObject(
    ValidationExpectations& expectations,
    const JsonValue& object,
    const std::map<std::string, double>& symbols,
    const KeyPointMap& keyPoints,
    const std::string& label,
    double defaultTolerance)
{
    if (!object.IsObject())
    {
        throw std::runtime_error(label + " must be an object");
    }
    if (const auto value = JsonFind(object, "min_result_bodies"))
    {
        expectations.minResultBodies = JsonInteger(*value, symbols, label + ".min_result_bodies");
    }
    if (const auto value = JsonFind(object, "max_result_bodies"))
    {
        expectations.maxResultBodiesSet = true;
        expectations.maxResultBodies = JsonInteger(*value, symbols, label + ".max_result_bodies");
    }
    if (const auto resultBodies = JsonFind(object, "result_bodies"))
    {
        if (!resultBodies->IsObject())
        {
            throw std::runtime_error(label + ".result_bodies must be an object");
        }
        if (const auto value = JsonFind(*resultBodies, "min"))
        {
            expectations.minResultBodies = JsonInteger(*value, symbols, label + ".result_bodies.min");
        }
        if (const auto value = JsonFind(*resultBodies, "max"))
        {
            expectations.maxResultBodiesSet = true;
            expectations.maxResultBodies = JsonInteger(*value, symbols, label + ".result_bodies.max");
        }
    }
    if (const auto value = JsonFind(object, "require_property_calculations"))
    {
        expectations.requirePropertyCalculations = JsonBool(*value, label + ".require_property_calculations");
    }
    if (const auto value = JsonFind(object, "require_finite_properties"))
    {
        expectations.requireFiniteProperties = JsonBool(*value, label + ".require_finite_properties");
    }
    if (const auto value = JsonFind(object, "require_nonnegative_length_area"))
    {
        expectations.requireNonnegativeLengthArea = JsonBool(*value, label + ".require_nonnegative_length_area");
    }
    if (const auto value = JsonFind(object, "require_nonnegative_volume"))
    {
        expectations.requireNonnegativeVolume = JsonBool(*value, label + ".require_nonnegative_volume");
    }
    if (const auto value = JsonFind(object, "boolean_volume_relation"))
    {
        expectations.booleanVolumeRelation = JsonBool(*value, label + ".boolean_volume_relation");
    }
    if (const auto value = JsonFind(object, "boolean_bbox_relation"))
    {
        expectations.booleanBboxRelation = JsonBool(*value, label + ".boolean_bbox_relation");
    }
    if (const auto value = JsonFind(object, "sample_input_properties"))
    {
        expectations.sampleInputProperties = JsonBool(*value, label + ".sample_input_properties");
    }
    if (const auto value = JsonFind(object, "volume_relation_abs_tol"))
    {
        expectations.relationAbsTol = JsonNumber(*value, symbols, label + ".volume_relation_abs_tol");
    }
    if (const auto value = JsonFind(object, "volume_relation_rel_tol"))
    {
        expectations.relationRelTol = JsonNumber(*value, symbols, label + ".volume_relation_rel_tol");
    }
    if (const auto value = JsonFind(object, "total_length"))
    {
        ApplyNumericExpectation(expectations.totalLength, *value, symbols, label + ".total_length");
    }
    if (const auto value = JsonFind(object, "total_area"))
    {
        ApplyNumericExpectation(expectations.totalArea, *value, symbols, label + ".total_area");
    }
    if (const auto value = JsonFind(object, "total_volume"))
    {
        ApplyNumericExpectation(expectations.totalVolume, *value, symbols, label + ".total_volume");
    }
    if (const auto value = JsonFind(object, "total_abs_volume"))
    {
        ApplyNumericExpectation(expectations.totalAbsVolume, *value, symbols, label + ".total_abs_volume");
    }
    if (const auto value = JsonFind(object, "point_relations"))
    {
        expectations.pointRelations = ParsePointRelations(*value, symbols, keyPoints, label + ".point_relations", defaultTolerance);
    }
    if (const auto value = JsonFind(object, "face_point_relations"))
    {
        expectations.facePointRelations = ParseFacePointRelations(*value, symbols, keyPoints, label + ".face_point_relations", defaultTolerance);
    }
    if (const auto value = JsonFind(object, "clash_checks"))
    {
        expectations.clashChecks = ParseClashChecks(*value, symbols, label + ".clash_checks", defaultTolerance);
    }
    if (const auto value = JsonFind(object, "distance_checks"))
    {
        expectations.distanceChecks = ParseDistanceChecks(*value, symbols, label + ".distance_checks", defaultTolerance);
    }
    if (const auto value = JsonFind(object, "plane_extreme_checks"))
    {
        expectations.planeExtremeChecks = ParsePlaneExtremeChecks(*value, symbols, label + ".plane_extreme_checks", defaultTolerance);
    }
    ApplyMetricShorthand(object, symbols, "total_length", expectations.totalLength);
    ApplyMetricShorthand(object, symbols, "total_area", expectations.totalArea);
    ApplyMetricShorthand(object, symbols, "total_volume", expectations.totalVolume);
    ApplyMetricShorthand(object, symbols, "total_abs_volume", expectations.totalAbsVolume);
}

void ApplyValidationExpectations(
    CaseRecipe& recipe,
    const JsonValue& object,
    const std::map<std::string, double>& symbols,
    const KeyPointMap& keyPoints,
    const std::string& label)
{
    if (const auto expectations = JsonFind(object, "expectations"))
    {
        ApplyValidationExpectationsObject(recipe.expectations, *expectations, symbols, keyPoints, label + ".expectations", recipe.modelingTol);
    }
    ApplyValidationExpectationsObject(recipe.expectations, object, symbols, keyPoints, label, recipe.modelingTol);
    if (recipe.expectations.minResultBodies < 0)
    {
        throw std::runtime_error(label + ".min_result_bodies must be >= 0");
    }
    if (recipe.expectations.maxResultBodiesSet && recipe.expectations.maxResultBodies < 0)
    {
        throw std::runtime_error(label + ".max_result_bodies must be >= 0");
    }
}

void ApplyCountExpectation(
    CountExpectation& expectation,
    const JsonValue& value,
    const std::map<std::string, double>& symbols,
    const std::string& label)
{
    if (value.IsObject())
    {
        if (const auto minValue = JsonFind(value, "min"))
        {
            expectation.minSet = true;
            expectation.min = JsonInteger(*minValue, symbols, label + ".min");
        }
        if (const auto maxValue = JsonFind(value, "max"))
        {
            expectation.maxSet = true;
            expectation.max = JsonInteger(*maxValue, symbols, label + ".max");
        }
    }
    else
    {
        expectation.minSet = true;
        expectation.maxSet = true;
        expectation.min = JsonInteger(value, symbols, label);
        expectation.max = expectation.min;
    }
    if (expectation.minSet && expectation.min < 0)
    {
        throw std::runtime_error(label + ".min must be >= 0");
    }
    if (expectation.maxSet && expectation.max < 0)
    {
        throw std::runtime_error(label + ".max must be >= 0");
    }
    if (expectation.minSet && expectation.maxSet && expectation.max < expectation.min)
    {
        throw std::runtime_error(label + ".max must be >= min");
    }
}

void LoadSplitExpectations(
    SplitRecipe& split,
    const JsonValue& object,
    const std::map<std::string, double>& symbols,
    const std::string& label)
{
    if (!object.IsObject())
    {
        throw std::runtime_error(label + " must be an object");
    }
    const std::array<std::pair<const char*, CountExpectation*>, 8> fields = {{
        {"split_outer_body_count", &split.outerBodies},
        {"split_inner_body_count", &split.innerBodies},
        {"split_wire_body_count", &split.wireBodies},
        {"split_total_body_count", &split.totalBodies},
        {"split_outer_bodies", &split.outerBodies},
        {"split_inner_bodies", &split.innerBodies},
        {"split_wire_bodies", &split.wireBodies},
        {"split_total_bodies", &split.totalBodies},
    }};
    for (const auto& field : fields)
    {
        if (const auto value = JsonFind(object, field.first))
        {
            ApplyCountExpectation(*field.second, *value, symbols, label + "." + field.first);
        }
    }
}

void LoadSplitRecipe(CaseRecipe& recipe, const JsonValue& root)
{
    const std::map<std::string, double> symbols = {{"pi", sggk::PI}, {"tau", sggk::PI2}};
    if (const auto field = JsonFind(root, "split_target_add_face"))
    {
        recipe.split.targetAddFace = JsonBool(*field, "split_target_add_face");
    }
    if (const auto field = JsonFind(root, "split_strict_split"))
    {
        recipe.split.strictSplit = JsonBool(*field, "split_strict_split");
    }
    if (const auto field = JsonFind(root, "split_merge_imprint"))
    {
        recipe.split.mergeImprint = JsonBool(*field, "split_merge_imprint");
    }
    LoadSplitExpectations(recipe.split, root, symbols, "recipe");
    if (const auto expectations = JsonFind(root, "expectations"))
    {
        LoadSplitExpectations(recipe.split, *expectations, symbols, "recipe.expectations");
    }
    if (const auto expectations = JsonFind(root, "split_expectations"))
    {
        LoadSplitExpectations(recipe.split, *expectations, symbols, "recipe.split_expectations");
    }
}

void LoadSliceExpectations(
    SliceRecipe& slice,
    const JsonValue& object,
    const std::map<std::string, double>& symbols,
    const std::string& label)
{
    if (!object.IsObject())
    {
        throw std::runtime_error(label + " must be an object");
    }
    const std::array<std::pair<const char*, CountExpectation*>, 4> fields = {{
        {"slice_result_body_count", &slice.resultBodies},
        {"slice_wire_body_count", &slice.wireBodies},
        {"slice_result_bodies", &slice.resultBodies},
        {"slice_wire_bodies", &slice.wireBodies},
    }};
    for (const auto& field : fields)
    {
        if (const auto value = JsonFind(object, field.first))
        {
            ApplyCountExpectation(*field.second, *value, symbols, label + "." + field.first);
        }
    }
}

void LoadSliceRecipe(CaseRecipe& recipe, const JsonValue& root)
{
    const std::map<std::string, double> symbols = {{"pi", sggk::PI}, {"tau", sggk::PI2}};
    LoadSliceExpectations(recipe.slice, root, symbols, "recipe");
    if (const auto expectations = JsonFind(root, "expectations"))
    {
        LoadSliceExpectations(recipe.slice, *expectations, symbols, "recipe.expectations");
    }
    if (const auto expectations = JsonFind(root, "slice_expectations"))
    {
        LoadSliceExpectations(recipe.slice, *expectations, symbols, "recipe.slice_expectations");
    }
}

void LoadTopologySectionExpectations(
    TopologySectionRecipe& section,
    const JsonValue& object,
    const std::map<std::string, double>& symbols,
    const std::string& label)
{
    if (!object.IsObject())
    {
        throw std::runtime_error(label + " must be an object");
    }
    const std::array<std::pair<const char*, CountExpectation*>, 6> fields = {{
        {"topology_section_edge_count", &section.edges},
        {"topology_section_vertex_count", &section.vertices},
        {"topology_section_total_count", &section.total},
        {"topology_section_edges", &section.edges},
        {"topology_section_vertices", &section.vertices},
        {"topology_section_total", &section.total},
    }};
    for (const auto& field : fields)
    {
        if (const auto value = JsonFind(object, field.first))
        {
            ApplyCountExpectation(*field.second, *value, symbols, label + "." + field.first);
        }
    }
}

void LoadTopologySectionRecipe(CaseRecipe& recipe, const JsonValue& root)
{
    const std::map<std::string, double> symbols = {{"pi", sggk::PI}, {"tau", sggk::PI2}};
    LoadTopologySectionExpectations(recipe.topologySection, root, symbols, "recipe");
    if (const auto expectations = JsonFind(root, "expectations"))
    {
        LoadTopologySectionExpectations(
            recipe.topologySection,
            *expectations,
            symbols,
            "recipe.expectations");
    }
    if (const auto expectations = JsonFind(root, "topology_section_expectations"))
    {
        LoadTopologySectionExpectations(
            recipe.topologySection,
            *expectations,
            symbols,
            "recipe.topology_section_expectations");
    }
}

bool FindString(const std::string& json, const std::string& key, std::string& value)
{
    const std::regex pattern("\"" + key + "\"\\s*:\\s*\"([^\"]*)\"");
    std::smatch match;
    if (!std::regex_search(json, match, pattern))
    {
        return false;
    }
    value = match[1].str();
    return true;
}

bool FindDouble(const std::string& json, const std::string& key, double& value)
{
    const std::regex pattern("\"" + key + "\"\\s*:\\s*(-?(?:\\d+\\.?\\d*|\\d*\\.\\d+)(?:[eE][+-]?\\d+)?)");
    std::smatch match;
    if (!std::regex_search(json, match, pattern))
    {
        return false;
    }
    value = std::stod(match[1].str());
    return true;
}

bool FindInt(const std::string& json, const std::string& key, int& value)
{
    const std::regex pattern("\"" + key + "\"\\s*:\\s*(-?\\d+)");
    std::smatch match;
    if (!std::regex_search(json, match, pattern))
    {
        return false;
    }
    value = std::stoi(match[1].str());
    return true;
}

bool FindBool(const std::string& json, const std::string& key, bool& value)
{
    const std::regex pattern("\"" + key + "\"\\s*:\\s*(true|false)");
    std::smatch match;
    if (!std::regex_search(json, match, pattern))
    {
        return false;
    }
    value = match[1].str() == "true";
    return true;
}

void LoadBodyOperations(const JsonValue& root, const std::string& prefix, BodySpec& spec)
{
    const auto operations = JsonFind(root, prefix + "_operations");
    if (!operations)
    {
        return;
    }
    if (!operations->IsArray())
    {
        throw std::runtime_error(prefix + "_operations must be an array");
    }
    spec.operations.clear();
    for (size_t index = 0; index < operations->arrayValue.size(); ++index)
    {
        spec.operations.push_back(JsonString(operations->arrayValue[index], prefix + "_operations." + std::to_string(index)));
    }
}

void LoadBodySpec(const std::string& json, const JsonValue& root, const std::string& prefix, BodySpec& spec)
{
    FindString(json, prefix + "_kind", spec.kind);
    FindString(json, prefix + "_boolean_type", spec.booleanType);
    FindDouble(json, prefix + "_radius", spec.radius);
    FindDouble(json, prefix + "_height", spec.height);
    FindDouble(json, prefix + "_angle", spec.angle);
    FindBool(json, prefix + "_create_seam_edge", spec.createSeamEdge);
    FindDouble(json, prefix + "_length", spec.length);
    FindDouble(json, prefix + "_width", spec.width);
    FindDouble(json, prefix + "_bottom_radius", spec.bottomRadius);
    FindDouble(json, prefix + "_top_radius", spec.topRadius);
    FindDouble(json, prefix + "_inner_radius", spec.innerRadius);
    FindDouble(json, prefix + "_outer_radius", spec.outerRadius);
    FindDouble(json, prefix + "_long_radius", spec.longRadius);
    FindDouble(json, prefix + "_short_radius", spec.shortRadius);
    FindDouble(json, prefix + "_profile_radius", spec.profileRadius);
    FindDouble(json, prefix + "_path_radius", spec.pathRadius);
    FindDouble(json, prefix + "_secondary_height", spec.secondaryHeight);
    FindDouble(json, prefix + "_secondary_translate_x", spec.secondaryTranslateX);
    FindDouble(json, prefix + "_secondary_translate_y", spec.secondaryTranslateY);
    FindDouble(json, prefix + "_secondary_translate_z", spec.secondaryTranslateZ);
    FindDouble(json, prefix + "_min_dist", spec.minDist);
    FindDouble(json, prefix + "_max_dist", spec.maxDist);
    FindDouble(json, prefix + "_operation_tol", spec.operationTol);
    FindDouble(json, prefix + "_g1_tol", spec.g1Tol);
    FindBool(json, prefix + "_allow_partial_success", spec.allowPartialSuccess);
    FindDouble(json, prefix + "_translate_x", spec.translateX);
    FindDouble(json, prefix + "_translate_y", spec.translateY);
    FindDouble(json, prefix + "_translate_z", spec.translateZ);
    FindDouble(json, prefix + "_scale", spec.scale);
    std::string sourceFile;
    if (FindString(json, prefix + "_source_file", sourceFile))
    {
        spec.sourceFile = sourceFile;
    }
    FindInt(json, prefix + "_body_index", spec.bodyIndex);
    LoadBodyOperations(root, prefix, spec);
}

void LoadOffset2DSegment(
    Offset2DSegmentSpec& segment,
    const JsonValue& value,
    const std::map<std::string, double>& symbols,
    const std::string& label)
{
    if (!value.IsObject())
    {
        throw std::runtime_error(label + " must be an object");
    }
    if (const auto field = JsonFind(value, "kind"))
    {
        segment.kind = JsonString(*field, label + ".kind");
    }
    if (const auto field = JsonFind(value, "sense"))
    {
        segment.sense = JsonBool(*field, label + ".sense");
    }
    if (const auto field = JsonFind(value, "start"))
    {
        const auto point = JsonPoint2Array(*field, symbols, label + ".start");
        segment.x1 = point[0];
        segment.y1 = point[1];
    }
    if (const auto field = JsonFind(value, "end"))
    {
        const auto point = JsonPoint2Array(*field, symbols, label + ".end");
        segment.x2 = point[0];
        segment.y2 = point[1];
    }
    if (const auto field = JsonFind(value, "center"))
    {
        const auto point = JsonPoint2Array(*field, symbols, label + ".center");
        segment.centerX = point[0];
        segment.centerY = point[1];
    }
    if (const auto field = JsonFind(value, "radius"))
    {
        segment.radius = JsonNumber(*field, symbols, label + ".radius");
    }
    if (const auto field = JsonFind(value, "start_angle"))
    {
        segment.startAngle = JsonNumber(*field, symbols, label + ".start_angle");
    }
    if (const auto field = JsonFind(value, "end_angle"))
    {
        segment.endAngle = JsonNumber(*field, symbols, label + ".end_angle");
    }
    if (const auto field = JsonFind(value, "ccw"))
    {
        segment.ccw = JsonBool(*field, label + ".ccw");
    }
    if (segment.kind != "line" && segment.kind != "arc")
    {
        throw std::runtime_error(label + ".kind must be line or arc");
    }
    if (segment.kind == "arc" && segment.radius <= 0.0)
    {
        throw std::runtime_error(label + ".radius must be > 0");
    }
    if (segment.kind == "line" && segment.x1 == segment.x2 && segment.y1 == segment.y2)
    {
        throw std::runtime_error(label + " line start and end must differ");
    }
}

void LoadOffset2DExpectations(
    Offset2DRecipe& offset,
    const JsonValue& object,
    const std::map<std::string, double>& symbols,
    const std::string& label)
{
    if (!object.IsObject())
    {
        throw std::runtime_error(label + " must be an object");
    }
    if (const auto field = JsonFind(object, "offset2d_status"))
    {
        offset.expectedStatus = JsonString(*field, label + ".offset2d_status");
    }
    if (const auto field = JsonFind(object, "offset2d_result_path_count"))
    {
        offset.resultPathCountMinSet = true;
        offset.resultPathCountMaxSet = true;
        offset.resultPathCountMin = JsonInteger(*field, symbols, label + ".offset2d_result_path_count");
        offset.resultPathCountMax = offset.resultPathCountMin;
    }
    if (const auto field = JsonFind(object, "offset2d_result_paths"))
    {
        if (!field->IsObject())
        {
            throw std::runtime_error(label + ".offset2d_result_paths must be an object");
        }
        if (const auto minValue = JsonFind(*field, "min"))
        {
            offset.resultPathCountMinSet = true;
            offset.resultPathCountMin = JsonInteger(*minValue, symbols, label + ".offset2d_result_paths.min");
        }
        if (const auto maxValue = JsonFind(*field, "max"))
        {
            offset.resultPathCountMaxSet = true;
            offset.resultPathCountMax = JsonInteger(*maxValue, symbols, label + ".offset2d_result_paths.max");
        }
    }
    if (offset.resultPathCountMinSet && offset.resultPathCountMin < 0)
    {
        throw std::runtime_error(label + ".offset2d_result_paths.min must be >= 0");
    }
    if (offset.resultPathCountMaxSet && offset.resultPathCountMax < 0)
    {
        throw std::runtime_error(label + ".offset2d_result_paths.max must be >= 0");
    }
    if (offset.resultPathCountMinSet && offset.resultPathCountMaxSet &&
        offset.resultPathCountMax < offset.resultPathCountMin)
    {
        throw std::runtime_error(label + ".offset2d_result_paths.max must be >= min");
    }
}

void LoadOffset2DRecipe(CaseRecipe& recipe, const JsonValue& root)
{
    const std::map<std::string, double> symbols = {{"pi", sggk::PI}, {"tau", sggk::PI2}};
    if (const auto field = JsonFind(root, "offset2d_distance"))
    {
        recipe.offset2d.distance = JsonNumber(*field, symbols, "offset2d_distance");
    }
    if (const auto field = JsonFind(root, "offset2d_distances"))
    {
        if (!field->IsArray())
        {
            throw std::runtime_error("offset2d_distances must be an array");
        }
        recipe.offset2d.distances.clear();
        for (size_t i = 0; i < field->arrayValue.size(); ++i)
        {
            recipe.offset2d.distances.push_back(
                JsonNumber(field->arrayValue[i], symbols, "offset2d_distances." + std::to_string(i)));
        }
    }
    if (const auto field = JsonFind(root, "offset2d_dist_tol"))
    {
        recipe.offset2d.distTol = JsonNumber(*field, symbols, "offset2d_dist_tol");
    }
    if (const auto field = JsonFind(root, "offset2d_angle_tol"))
    {
        recipe.offset2d.angleTol = JsonNumber(*field, symbols, "offset2d_angle_tol");
    }
    if (const auto field = JsonFind(root, "offset2d_connect_type"))
    {
        recipe.offset2d.connectType = JsonString(*field, "offset2d_connect_type");
    }
    if (const auto field = JsonFind(root, "offset2d_allow_crv_degenerated"))
    {
        recipe.offset2d.allowCrvDegenerated = JsonBool(*field, "offset2d_allow_crv_degenerated");
    }
    if (const auto field = JsonFind(root, "offset2d_allow_crv_reversed"))
    {
        recipe.offset2d.allowCrvReversed = JsonBool(*field, "offset2d_allow_crv_reversed");
    }
    if (const auto field = JsonFind(root, "offset2d_allow_self_intersections"))
    {
        recipe.offset2d.allowSelfIntersections = JsonBool(*field, "offset2d_allow_self_intersections");
    }
    if (const auto field = JsonFind(root, "offset2d_extend_type"))
    {
        recipe.offset2d.extendType = JsonString(*field, "offset2d_extend_type");
    }

    const JsonValue* path = JsonFind(root, "offset2d_path");
    if (!path)
    {
        path = JsonFind(root, "offset2d_segments");
    }
    if (path)
    {
        if (!path->IsArray() || path->arrayValue.empty())
        {
            throw std::runtime_error("offset2d_path must be a non-empty array");
        }
        recipe.offset2d.path.clear();
        for (size_t i = 0; i < path->arrayValue.size(); ++i)
        {
            Offset2DSegmentSpec segment;
            LoadOffset2DSegment(segment, path->arrayValue[i], symbols, "offset2d_path." + std::to_string(i));
            recipe.offset2d.path.push_back(segment);
        }
    }
    if (recipe.offset2d.path.empty())
    {
        throw std::runtime_error("api_offset2d requires offset2d_path");
    }
    if (!recipe.offset2d.distances.empty() && recipe.offset2d.distances.size() != recipe.offset2d.path.size())
    {
        throw std::runtime_error("offset2d_distances size must match offset2d_path size");
    }
    if (recipe.offset2d.distTol <= 0.0)
    {
        throw std::runtime_error("offset2d_dist_tol must be > 0");
    }
    if (recipe.offset2d.angleTol <= 0.0)
    {
        throw std::runtime_error("offset2d_angle_tol must be > 0");
    }
    LoadOffset2DExpectations(recipe.offset2d, root, symbols, "recipe");
    if (const auto expectations = JsonFind(root, "expectations"))
    {
        LoadOffset2DExpectations(recipe.offset2d, *expectations, symbols, "recipe.expectations");
    }
    if (const auto expectations = JsonFind(root, "offset2d_expectations"))
    {
        LoadOffset2DExpectations(recipe.offset2d, *expectations, symbols, "recipe.offset2d_expectations");
    }
}

bool IsSafeCaseId(const std::string& value)
{
    if (value.empty() || value.size() > 128 || !std::isalnum(static_cast<unsigned char>(value.front())))
    {
        return false;
    }
    return std::all_of(value.begin(), value.end(), [](const char ch) {
        const auto uch = static_cast<unsigned char>(ch);
        return std::isalnum(uch) || ch == '_' || ch == '-';
    });
}

void RequireSafeCaseId(const std::string& value)
{
    if (!IsSafeCaseId(value))
    {
        throw std::runtime_error("case_id must match ^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$");
    }
}

fs::path CaseDirectory(const fs::path& outRoot, const std::string& caseId)
{
    RequireSafeCaseId(caseId);
    const fs::path root = fs::absolute(outRoot).lexically_normal();
    const fs::path candidate = (root / caseId).lexically_normal();
    if (candidate.parent_path() != root)
    {
        throw std::runtime_error("case output escaped --out root");
    }
    return candidate;
}

CaseRecipe LoadRecipe(const fs::path& path)
{
    CaseRecipe recipe;
    if (path.empty())
    {
        return recipe;
    }

    const std::string json = ReadTextFile(path);
    JsonParser parser(json);
    const JsonValue root = parser.Parse();
    if (!root.IsObject())
    {
        throw std::runtime_error("recipe root must be an object");
    }
    FindString(json, "case_id", recipe.caseId);
    RequireSafeCaseId(recipe.caseId);
    FindString(json, "api", recipe.api);
    FindString(json, "hypothesis", recipe.hypothesis);
    FindString(json, "dsl_source", recipe.dslSource);
    FindString(json, "dsl_case_id", recipe.dslCaseId);
    FindString(json, "dsl_variant", recipe.dslVariant);
    FindString(json, "source_ref", recipe.sourceRef);
    FindString(json, "source_task_id", recipe.sourceTaskId);
    FindString(json, "source_task_path", recipe.sourceTaskPath);
    FindString(json, "source_risk_id", recipe.sourceRiskId);
    FindString(json, "source_risk_family", recipe.sourceRiskFamily);
    FindString(json, "source_risk_categories", recipe.sourceRiskCategories);
    const bool booleanTypeProvided = FindString(json, "boolean_type", recipe.booleanType);
    if (recipe.api == "api_boolean_slice" && !booleanTypeProvided)
    {
        recipe.booleanType = "UNION";
    }
    FindDouble(json, "modeling_tol", recipe.modelingTol);
    FindDouble(json, "offset_distance", recipe.offsetDistance);
    FindDouble(json, "max_model_size", recipe.maxModelSize);
    if (recipe.maxModelSize <= 0.0)
    {
        throw std::runtime_error("max_model_size must be > 0");
    }
    FindBool(json, "check_valid", recipe.checkValid);
    FindBool(json, "topo_track", recipe.topoTrack);
    FindBool(json, "non_destructive", recipe.nonDestructive);
    LoadBodySpec(json, root, "target", recipe.boolean.target);
    LoadBodySpec(json, root, "tool", recipe.boolean.tool);
    LoadBodySpec(json, root, "source", recipe.offsetSource);
    std::string sourceFile;
    if (FindString(json, "source_file", sourceFile))
    {
        recipe.sourceFile = sourceFile;
    }
    FindInt(json, "body_index", recipe.sourceBodyIndex);
    FindInt(json, "source_body_index", recipe.sourceBodyIndex);
    if (!recipe.sourceFile.empty() && recipe.offsetSource.sourceFile.empty())
    {
        recipe.offsetSource.sourceFile = recipe.sourceFile;
    }
    recipe.offsetSource.bodyIndex = recipe.sourceBodyIndex;
    FindString(json, "step_app_protocol", recipe.stepAppProtocol);
    FindBool(json, "step_surface_to_bspline", recipe.stepSurfaceToBSpline);
    FindBool(json, "step_curve_to_bspline", recipe.stepCurveToBSpline);
    FindBool(json, "step_spcurve_in_wire_to_bspline", recipe.stepSpcurveInWireToBSpline);
    FindBool(json, "iges_face_only_mode", recipe.igesFaceOnlyMode);
    FindBool(json, "iges_write_sgk_specified_data", recipe.igesWriteSGKSpecifiedData);
    FindDouble(json, "roundtrip_abs_tol", recipe.roundtripAbsTol);
    FindDouble(json, "roundtrip_rel_tol", recipe.roundtripRelTol);
    ApplyValidationExpectations(recipe, root, {}, {}, "recipe");
    if (recipe.api == "api_boolean_split")
    {
        LoadSplitRecipe(recipe, root);
    }
    else if (recipe.api == "api_boolean_slice")
    {
        LoadSliceRecipe(recipe, root);
    }
    else if (recipe.api == "api_offset2d")
    {
        LoadOffset2DRecipe(recipe, root);
    }
    else if (recipe.api == "api_topology_section")
    {
        LoadTopologySectionRecipe(recipe, root);
    }
    return recipe;
}

std::string SanitizeCaseId(const std::string& raw)
{
    std::string result;
    bool previousUnderscore = false;
    for (const char ch : raw)
    {
        if (std::isalnum(static_cast<unsigned char>(ch)))
        {
            result.push_back(static_cast<char>(std::tolower(static_cast<unsigned char>(ch))));
            previousUnderscore = false;
        }
        else if (!previousUnderscore)
        {
            result.push_back('_');
            previousUnderscore = true;
        }
    }
    while (!result.empty() && result.front() == '_')
    {
        result.erase(result.begin());
    }
    while (!result.empty() && result.back() == '_')
    {
        result.pop_back();
    }
    if (result.empty())
    {
        throw std::runtime_error("empty DSL case id");
    }
    return result;
}

std::map<std::string, double> ResolveDslConstants(const JsonValue& root)
{
    std::map<std::string, double> symbols;
    symbols["pi"] = sggk::PI;
    symbols["tau"] = sggk::PI2;

    const auto constants = JsonFind(root, "constants");
    if (!constants)
    {
        return symbols;
    }
    if (!constants->IsObject())
    {
        throw std::runtime_error("DSL constants must be an object");
    }

    std::map<std::string, JsonValue> pending = constants->objectValue;
    while (!pending.empty())
    {
        bool progressed = false;
        for (auto it = pending.begin(); it != pending.end();)
        {
            try
            {
                symbols[it->first] = JsonNumber(it->second, symbols, "constants." + it->first);
                it = pending.erase(it);
                progressed = true;
            }
            catch (const std::exception&)
            {
                ++it;
            }
        }
        if (!progressed)
        {
            std::ostringstream os;
            os << "could not resolve DSL constants:";
            for (const auto& item : pending)
            {
                os << " " << item.first;
            }
            throw std::runtime_error(os.str());
        }
    }
    return symbols;
}

void ApplyDslOptions(
    CaseRecipe& recipe,
    const JsonValue& options,
    const std::map<std::string, double>& symbols,
    const KeyPointMap& keyPoints)
{
    if (!options.IsObject())
    {
        throw std::runtime_error("DSL options/defaults must be an object");
    }
    if (const auto value = JsonFind(options, "api"))
    {
        recipe.api = JsonString(*value, "api");
    }
    if (const auto value = JsonFind(options, "boolean_type"))
    {
        recipe.booleanType = JsonString(*value, "boolean_type");
    }
    if (const auto value = JsonFind(options, "modeling_tol"))
    {
        recipe.modelingTol = JsonNumber(*value, symbols, "modeling_tol");
    }
    if (const auto value = JsonFind(options, "max_model_size"))
    {
        recipe.maxModelSize = JsonNumber(*value, symbols, "max_model_size");
        if (recipe.maxModelSize <= 0.0)
        {
            throw std::runtime_error("max_model_size must be > 0");
        }
    }
    if (const auto value = JsonFind(options, "check_valid"))
    {
        recipe.checkValid = JsonBool(*value, "check_valid");
    }
    if (const auto value = JsonFind(options, "topo_track"))
    {
        recipe.topoTrack = JsonBool(*value, "topo_track");
    }
    if (const auto value = JsonFind(options, "non_destructive"))
    {
        recipe.nonDestructive = JsonBool(*value, "non_destructive");
    }
    if (const auto value = JsonFind(options, "body_index"))
    {
        recipe.sourceBodyIndex = JsonInteger(*value, symbols, "body_index");
    }
    if (const auto value = JsonFind(options, "source_body_index"))
    {
        recipe.sourceBodyIndex = JsonInteger(*value, symbols, "source_body_index");
    }
    if (const auto value = JsonFind(options, "step_app_protocol"))
    {
        recipe.stepAppProtocol = JsonString(*value, "step_app_protocol");
    }
    if (const auto value = JsonFind(options, "step_surface_to_bspline"))
    {
        recipe.stepSurfaceToBSpline = JsonBool(*value, "step_surface_to_bspline");
    }
    if (const auto value = JsonFind(options, "step_curve_to_bspline"))
    {
        recipe.stepCurveToBSpline = JsonBool(*value, "step_curve_to_bspline");
    }
    if (const auto value = JsonFind(options, "step_spcurve_in_wire_to_bspline"))
    {
        recipe.stepSpcurveInWireToBSpline = JsonBool(*value, "step_spcurve_in_wire_to_bspline");
    }
    if (const auto value = JsonFind(options, "iges_face_only_mode"))
    {
        recipe.igesFaceOnlyMode = JsonBool(*value, "iges_face_only_mode");
    }
    if (const auto value = JsonFind(options, "iges_write_sgk_specified_data"))
    {
        recipe.igesWriteSGKSpecifiedData = JsonBool(*value, "iges_write_sgk_specified_data");
    }
    if (const auto value = JsonFind(options, "roundtrip_abs_tol"))
    {
        recipe.roundtripAbsTol = JsonNumber(*value, symbols, "roundtrip_abs_tol");
    }
    if (const auto value = JsonFind(options, "roundtrip_rel_tol"))
    {
        recipe.roundtripRelTol = JsonNumber(*value, symbols, "roundtrip_rel_tol");
    }
    ApplyValidationExpectations(recipe, options, symbols, keyPoints, "options");
}

void ApplyDslCaseOptions(
    CaseRecipe& recipe,
    const JsonValue& dslCase,
    const std::map<std::string, double>& symbols,
    const KeyPointMap& keyPoints)
{
    ApplyDslOptions(recipe, dslCase, symbols, keyPoints);
    if (const auto options = JsonFind(dslCase, "options"))
    {
        ApplyDslOptions(recipe, *options, symbols, keyPoints);
    }
    ApplyValidationExpectations(recipe, dslCase, symbols, keyPoints, "case");
}

std::string DslStringOrDefault(const JsonValue& object, const std::string& key, const std::string& fallback)
{
    if (const auto value = JsonFind(object, key))
    {
        return JsonString(*value, key);
    }
    return fallback;
}

std::string DslStringFromCaseOrMetadata(const JsonValue& object, const std::string& key)
{
    if (const auto value = JsonFind(object, key))
    {
        return JsonString(*value, key);
    }
    if (const auto metadata = JsonFind(object, "metadata"))
    {
        if (metadata->IsObject())
        {
            if (const auto value = JsonFind(*metadata, key))
            {
                return JsonString(*value, "metadata." + key);
            }
        }
    }
    return "";
}

std::string DslOperationId(const JsonValue& step, size_t index, const std::string& op)
{
    if (const auto value = JsonFind(step, "id"))
    {
        return JsonString(*value, "id");
    }
    return "op_" + std::to_string(index + 1) + "_" + op;
}

bool DslBoolOrDefault(const JsonValue& object, const std::string& key, bool fallback)
{
    if (const auto value = JsonFind(object, key))
    {
        return JsonBool(*value, key);
    }
    return fallback;
}

double DslNumberOrDefault(
    const JsonValue& object,
    const std::string& key,
    double fallback,
    const std::map<std::string, double>& symbols)
{
    if (const auto value = JsonFind(object, key))
    {
        return JsonNumber(*value, symbols, key);
    }
    return fallback;
}

void ReadDslVector3(
    const JsonValue& object,
    const std::string& key,
    double& x,
    double& y,
    double& z,
    const std::map<std::string, double>& symbols)
{
    const auto value = JsonFind(object, key);
    if (!value)
    {
        return;
    }
    if (!value->IsArray() || value->arrayValue.size() != 3)
    {
        throw std::runtime_error(key + " must be a 3-number array");
    }
    x = JsonNumber(value->arrayValue[0], symbols, key + ".0");
    y = JsonNumber(value->arrayValue[1], symbols, key + ".1");
    z = JsonNumber(value->arrayValue[2], symbols, key + ".2");
}

BodySpec BodySpecFromJson(const JsonValue& body, const std::map<std::string, double>& symbols, const std::string& role);

BodySpec BodySpecFieldsFromJson(
    const JsonValue& body,
    const std::map<std::string, double>& symbols,
    const std::string& role)
{
    if (!body.IsObject())
    {
        throw std::runtime_error(role + " body must be an object");
    }

    BodySpec spec;
    spec.kind = DslStringOrDefault(body, "kind", "");
    if (spec.kind.empty())
    {
        throw std::runtime_error(role + " body requires kind");
    }
    spec.booleanType = DslStringOrDefault(body, "boolean_type", spec.booleanType);
    spec.radius = DslNumberOrDefault(body, "radius", spec.radius, symbols);
    spec.height = DslNumberOrDefault(body, "height", spec.height, symbols);
    spec.angle = DslNumberOrDefault(body, "angle", spec.angle, symbols);
    spec.createSeamEdge = DslBoolOrDefault(body, "create_seam_edge", spec.createSeamEdge);
    spec.length = DslNumberOrDefault(body, "length", spec.length, symbols);
    spec.width = DslNumberOrDefault(body, "width", spec.width, symbols);
    spec.bottomRadius = DslNumberOrDefault(body, "bottom_radius", spec.bottomRadius, symbols);
    spec.topRadius = DslNumberOrDefault(body, "top_radius", spec.topRadius, symbols);
    spec.innerRadius = DslNumberOrDefault(body, "inner_radius", spec.innerRadius, symbols);
    spec.outerRadius = DslNumberOrDefault(body, "outer_radius", spec.outerRadius, symbols);
    spec.longRadius = DslNumberOrDefault(body, "long_radius", spec.longRadius, symbols);
    spec.shortRadius = DslNumberOrDefault(body, "short_radius", spec.shortRadius, symbols);
    spec.profileRadius = DslNumberOrDefault(body, "profile_radius", spec.profileRadius, symbols);
    spec.profileRadius = DslNumberOrDefault(body, "radius", spec.profileRadius, symbols);
    spec.pathRadius = DslNumberOrDefault(body, "path_radius", spec.pathRadius, symbols);
    spec.secondaryHeight = DslNumberOrDefault(body, "secondary_height", spec.secondaryHeight, symbols);
    spec.minDist = DslNumberOrDefault(body, "min_dist", spec.minDist, symbols);
    spec.maxDist = DslNumberOrDefault(body, "max_dist", spec.maxDist, symbols);
    spec.operationTol = DslNumberOrDefault(body, "operation_tol", spec.operationTol, symbols);
    spec.g1Tol = DslNumberOrDefault(body, "g1_tol", spec.g1Tol, symbols);
    spec.allowPartialSuccess = DslBoolOrDefault(body, "allow_partial_success", spec.allowPartialSuccess);
    ReadDslVector3(body, "translate", spec.translateX, spec.translateY, spec.translateZ, symbols);
    spec.translateX = DslNumberOrDefault(body, "translate_x", spec.translateX, symbols);
    spec.translateY = DslNumberOrDefault(body, "translate_y", spec.translateY, symbols);
    spec.translateZ = DslNumberOrDefault(body, "translate_z", spec.translateZ, symbols);
    ReadDslVector3(body, "secondary_translate", spec.secondaryTranslateX, spec.secondaryTranslateY, spec.secondaryTranslateZ, symbols);
    spec.secondaryTranslateX = DslNumberOrDefault(body, "secondary_translate_x", spec.secondaryTranslateX, symbols);
    spec.secondaryTranslateY = DslNumberOrDefault(body, "secondary_translate_y", spec.secondaryTranslateY, symbols);
    spec.secondaryTranslateZ = DslNumberOrDefault(body, "secondary_translate_z", spec.secondaryTranslateZ, symbols);
    spec.scale = DslNumberOrDefault(body, "scale", spec.scale, symbols);
    if (const auto sourceFile = JsonFind(body, "source_file"))
    {
        spec.sourceFile = JsonString(*sourceFile, role + ".source_file");
    }
    spec.bodyIndex = static_cast<int>(DslNumberOrDefault(body, "body_index", spec.bodyIndex, symbols));
    if (const auto id = JsonFind(body, "id"))
    {
        spec.operations.push_back(JsonString(*id, role + ".id"));
    }
    return spec;
}

struct ProfileSpec
{
    std::string kind;
    double length = 0.0;
    double width = 0.0;
    double radius = 0.0;
    double bottomRadius = 0.0;
    double topRadius = 0.0;
    double innerRadius = 0.0;
    double outerRadius = 0.0;
    double height = 0.0;
    double operationTol = sggk::Precision::DefModelingTol;
    double g1Tol = 0.1;
    std::vector<std::string> operations;
};

void ApplyDslTransform(BodySpec& spec, const JsonValue& step, const std::map<std::string, double>& symbols)
{
    double tx = 0.0;
    double ty = 0.0;
    double tz = 0.0;
    ReadDslVector3(step, "translate", tx, ty, tz, symbols);
    tx = DslNumberOrDefault(step, "translate_x", tx, symbols);
    ty = DslNumberOrDefault(step, "translate_y", ty, symbols);
    tz = DslNumberOrDefault(step, "translate_z", tz, symbols);
    spec.translateX += tx;
    spec.translateY += ty;
    spec.translateZ += tz;
    spec.scale *= DslNumberOrDefault(step, "scale", 1.0, symbols);
}

void AppendOperations(std::vector<std::string>& target, const std::vector<std::string>& source)
{
    target.insert(target.end(), source.begin(), source.end());
}

void AppendOperation(std::vector<std::string>& target, const std::string& opId)
{
    if (opId.empty())
    {
        return;
    }
    if (target.empty() || target.back() != opId)
    {
        target.push_back(opId);
    }
}

BodySpec CompileDslBooleanChain(
    const BodySpec& base,
    const JsonValue& step,
    const std::map<std::string, double>& symbols,
    const std::string& role,
    const std::string& opId)
{
    const auto toolNode = JsonFind(step, "tool");
    if (!toolNode)
    {
        throw std::runtime_error(role + " boolean op requires tool");
    }
    const BodySpec tool = BodySpecFromJson(*toolNode, symbols, role + ".boolean.tool");
    const std::string booleanType = DslStringOrDefault(step, "boolean_type", DslStringOrDefault(step, "type", "SUBTRACTION"));

    if (base.kind == "solid_cylinder" && tool.kind == "solid_wedge")
    {
        if (std::fabs(base.translateX) > 0.0 || std::fabs(base.translateY) > 0.0 ||
            std::fabs(base.translateZ) > 0.0 || std::fabs(base.scale - 1.0) > 1e-15)
        {
            throw std::runtime_error(role + " native pre-boolean chain does not yet support transformed cylinder base");
        }
        if (std::fabs(tool.scale - 1.0) > 1e-15)
        {
            throw std::runtime_error(role + " native pre-boolean chain does not yet support scaled wedge tool");
        }

        BodySpec result;
        result.kind = "pre_boolean_cylinder_wedge";
        result.booleanType = booleanType;
        result.radius = base.radius;
        result.height = base.height;
        result.angle = base.angle;
        result.createSeamEdge = base.createSeamEdge;
        result.length = tool.length;
        result.width = tool.width;
        result.secondaryHeight = tool.height;
        result.secondaryTranslateX = tool.translateX;
        result.secondaryTranslateY = tool.translateY;
        result.secondaryTranslateZ = tool.translateZ;
        result.operationTol = DslNumberOrDefault(step, "operation_tol", base.operationTol, symbols);
        result.operations = base.operations;
        result.operations.push_back(opId);
        AppendOperations(result.operations, tool.operations);
        return result;
    }

    throw std::runtime_error(
        role + " unsupported native boolean chain pattern: " + base.kind + " " + booleanType + " " + tool.kind);
}

BodySpec CompileDslBodyChain(
    const JsonValue& chain,
    const std::map<std::string, double>& symbols,
    const std::string& role)
{
    if (!chain.IsArray() || chain.arrayValue.empty())
    {
        throw std::runtime_error(role + ".chain must be a non-empty array");
    }

    bool hasBody = false;
    bool hasProfile = false;
    BodySpec current;
    ProfileSpec profile;

    for (size_t index = 0; index < chain.arrayValue.size(); ++index)
    {
        const auto& step = chain.arrayValue[index];
        if (!step.IsObject())
        {
            throw std::runtime_error(role + ".chain step must be an object");
        }
        const std::string op = DslStringOrDefault(step, "op", "");
        const std::string opId = DslOperationId(step, index, op);
        if (op == "primitive")
        {
            current = BodySpecFieldsFromJson(step, symbols, role + ".primitive");
            AppendOperation(current.operations, opId);
            hasBody = true;
            hasProfile = false;
        }
        else if (op == "body")
        {
            const auto body = JsonFind(step, "body");
            if (!body)
            {
                throw std::runtime_error(role + ".body op requires body");
            }
            current = BodySpecFromJson(*body, symbols, role + ".body");
            AppendOperation(current.operations, opId);
            hasBody = true;
            hasProfile = false;
        }
        else if (op == "load_sgt")
        {
            current = BodySpec();
            current.kind = "loaded_sgt";
            const auto sourceFile = JsonFind(step, "source_file");
            if (!sourceFile)
            {
                throw std::runtime_error(role + ".load_sgt requires source_file");
            }
            current.sourceFile = JsonString(*sourceFile, role + ".load_sgt.source_file");
            current.bodyIndex = static_cast<int>(DslNumberOrDefault(step, "body_index", 0.0, symbols));
            AppendOperation(current.operations, opId);
            hasBody = true;
            hasProfile = false;
        }
        else if (op == "rect_profile")
        {
            profile = ProfileSpec();
            profile.kind = "rect_profile";
            profile.length = DslNumberOrDefault(step, "length", profile.length, symbols);
            profile.width = DslNumberOrDefault(step, "width", profile.width, symbols);
            profile.operationTol = DslNumberOrDefault(step, "operation_tol", profile.operationTol, symbols);
            profile.g1Tol = 0.0;
            AppendOperation(profile.operations, opId);
            hasProfile = true;
            hasBody = false;
        }
        else if (op == "circle_profile")
        {
            profile = ProfileSpec();
            profile.kind = "circle_profile";
            profile.radius = DslNumberOrDefault(step, "radius", profile.radius, symbols);
            profile.radius = DslNumberOrDefault(step, "profile_radius", profile.radius, symbols);
            profile.operationTol = DslNumberOrDefault(step, "operation_tol", profile.operationTol, symbols);
            profile.g1Tol = DslNumberOrDefault(step, "g1_tol", profile.g1Tol, symbols);
            AppendOperation(profile.operations, opId);
            hasProfile = true;
            hasBody = false;
        }
        else if (op == "line_profile")
        {
            profile = ProfileSpec();
            profile.kind = "line_profile";
            profile.bottomRadius = DslNumberOrDefault(step, "bottom_radius", profile.bottomRadius, symbols);
            profile.topRadius = DslNumberOrDefault(step, "top_radius", profile.topRadius, symbols);
            profile.height = DslNumberOrDefault(step, "height", profile.height, symbols);
            profile.operationTol = DslNumberOrDefault(step, "operation_tol", profile.operationTol, symbols);
            AppendOperation(profile.operations, opId);
            hasProfile = true;
            hasBody = false;
        }
        else if (op == "radial_rect_profile")
        {
            profile = ProfileSpec();
            profile.kind = "radial_rect_profile";
            profile.innerRadius = DslNumberOrDefault(step, "inner_radius", profile.innerRadius, symbols);
            profile.outerRadius = DslNumberOrDefault(step, "outer_radius", profile.outerRadius, symbols);
            profile.height = DslNumberOrDefault(step, "height", profile.height, symbols);
            profile.operationTol = DslNumberOrDefault(step, "operation_tol", profile.operationTol, symbols);
            AppendOperation(profile.operations, opId);
            hasProfile = true;
            hasBody = false;
        }
        else if (op == "extrude")
        {
            if (!hasProfile || profile.kind != "rect_profile")
            {
                throw std::runtime_error(role + ".extrude currently requires a preceding rect_profile");
            }
            current = BodySpec();
            current.kind = "extrude_rect";
            current.length = profile.length;
            current.width = profile.width;
            current.height = DslNumberOrDefault(step, "height", current.height, symbols);
            current.operationTol = DslNumberOrDefault(step, "operation_tol", current.operationTol, symbols);
            AppendOperations(current.operations, profile.operations);
            AppendOperation(current.operations, opId);
            hasBody = true;
            hasProfile = false;
        }
        else if (op == "thicken")
        {
            if (!hasProfile || profile.kind != "rect_profile")
            {
                throw std::runtime_error(role + ".thicken currently requires a preceding rect_profile");
            }
            current = BodySpec();
            current.kind = "thicken_rect_sheet";
            current.length = profile.length;
            current.width = profile.width;
            current.minDist = DslNumberOrDefault(step, "min_dist", current.minDist, symbols);
            current.maxDist = DslNumberOrDefault(step, "max_dist", current.maxDist, symbols);
            current.operationTol = DslNumberOrDefault(step, "operation_tol", profile.operationTol, symbols);
            current.g1Tol = DslNumberOrDefault(step, "g1_tol", current.g1Tol, symbols);
            current.allowPartialSuccess = DslBoolOrDefault(step, "allow_partial_success", current.allowPartialSuccess);
            AppendOperations(current.operations, profile.operations);
            AppendOperation(current.operations, opId);
            hasBody = true;
            hasProfile = false;
        }
        else if (op == "sweep_line")
        {
            if (!hasProfile || profile.kind != "circle_profile")
            {
                throw std::runtime_error(role + ".sweep_line currently requires a preceding circle_profile");
            }
            current = BodySpec();
            current.kind = "sweep_circle_line";
            current.profileRadius = profile.radius;
            current.height = DslNumberOrDefault(step, "height", current.height, symbols);
            current.operationTol = DslNumberOrDefault(step, "operation_tol", profile.operationTol, symbols);
            current.g1Tol = DslNumberOrDefault(step, "g1_tol", profile.g1Tol, symbols);
            AppendOperations(current.operations, profile.operations);
            AppendOperation(current.operations, opId);
            hasBody = true;
            hasProfile = false;
        }
        else if (op == "support_sweep" || op == "support_sweep_bspline_surface")
        {
            current = BodySpec();
            current.kind = "support_sweep_bspline_surface";
            current.pathRadius = DslNumberOrDefault(step, "path_radius", current.pathRadius, symbols);
            current.height = DslNumberOrDefault(step, "height", current.height, symbols);
            current.profileRadius = DslNumberOrDefault(step, "profile_radius", current.profileRadius, symbols);
            current.profileRadius = DslNumberOrDefault(step, "radius", current.profileRadius, symbols);
            current.operationTol = DslNumberOrDefault(step, "operation_tol", current.operationTol, symbols);
            current.g1Tol = DslNumberOrDefault(step, "g1_tol", current.g1Tol, symbols);
            AppendOperation(current.operations, opId);
            hasBody = true;
            hasProfile = false;
        }
        else if (op == "revolve")
        {
            if (!hasProfile || (profile.kind != "line_profile" && profile.kind != "radial_rect_profile"))
            {
                throw std::runtime_error(role + ".revolve currently requires a preceding line_profile or radial_rect_profile");
            }
            current = BodySpec();
            if (profile.kind == "line_profile")
            {
                current.kind = "revolve_line";
                current.bottomRadius = profile.bottomRadius;
                current.topRadius = profile.topRadius;
            }
            else
            {
                current.kind = "revolve_rect";
                current.innerRadius = profile.innerRadius;
                current.outerRadius = profile.outerRadius;
            }
            current.height = profile.height;
            current.angle = DslNumberOrDefault(step, "angle", current.angle, symbols);
            current.operationTol = DslNumberOrDefault(step, "operation_tol", profile.operationTol, symbols);
            AppendOperations(current.operations, profile.operations);
            AppendOperation(current.operations, opId);
            hasBody = true;
            hasProfile = false;
        }
        else if (op == "boolean")
        {
            if (!hasBody)
            {
                throw std::runtime_error(role + ".boolean requires an existing body");
            }
            current = CompileDslBooleanChain(current, step, symbols, role, opId);
            hasBody = true;
            hasProfile = false;
        }
        else if (op == "transform")
        {
            if (!hasBody)
            {
                throw std::runtime_error(role + ".transform requires an existing body");
            }
            ApplyDslTransform(current, step, symbols);
            AppendOperation(current.operations, opId);
        }
        else
        {
            throw std::runtime_error(role + " unsupported chain op: " + op);
        }
    }

    if (!hasBody)
    {
        throw std::runtime_error(role + ".chain did not produce a body");
    }
    return current;
}

BodySpec BodySpecFromJson(const JsonValue& body, const std::map<std::string, double>& symbols, const std::string& role)
{
    if (!body.IsObject())
    {
        throw std::runtime_error(role + " must be an object");
    }
    if (const auto chain = JsonFind(body, "chain"))
    {
        BodySpec spec = CompileDslBodyChain(*chain, symbols, role);
        ApplyDslTransform(spec, body, symbols);
        return spec;
    }
    return BodySpecFieldsFromJson(body, symbols, role);
}

void ApplyPatchPath(JsonValue& object, const std::string& path, const JsonValue& patchValue)
{
    JsonValue* cursor = &object;
    size_t start = 0;
    while (true)
    {
        const size_t dot = path.find('.', start);
        const std::string part = path.substr(start, dot == std::string::npos ? std::string::npos : dot - start);
        const bool leaf = dot == std::string::npos;
        if (part.empty())
        {
            throw std::runtime_error("empty DSL patch path segment");
        }

        if (cursor->IsArray())
        {
            const size_t index = static_cast<size_t>(std::stoul(part));
            if (index >= cursor->arrayValue.size())
            {
                throw std::runtime_error("DSL patch array index out of range: " + path);
            }
            if (leaf)
            {
                cursor->arrayValue[index] = patchValue;
                return;
            }
            cursor = &cursor->arrayValue[index];
        }
        else if (cursor->IsObject())
        {
            if (leaf)
            {
                cursor->objectValue[part] = patchValue;
                return;
            }
            cursor = &cursor->objectValue[part];
        }
        else
        {
            throw std::runtime_error("DSL patch traversed non-container: " + path);
        }
        start = dot + 1;
    }
}

std::vector<std::pair<JsonValue, std::string>> ExpandDslCase(const JsonValue& dslCase)
{
    std::vector<std::pair<JsonValue, std::string>> expanded;
    expanded.push_back({dslCase, ""});

    if (const auto variants = JsonFind(dslCase, "variants"))
    {
        if (!variants->IsArray())
        {
            throw std::runtime_error("DSL variants must be an array");
        }
        std::vector<std::pair<JsonValue, std::string>> next;
        for (size_t i = 0; i < variants->arrayValue.size(); ++i)
        {
            const auto& variant = variants->arrayValue[i];
            if (!variant.IsObject())
            {
                throw std::runtime_error("DSL variant must be an object");
            }
            const std::string suffix = SanitizeCaseId(DslStringOrDefault(variant, "suffix", "v" + std::to_string(i + 1)));
            const auto set = JsonFind(variant, "set");
            for (const auto& item : expanded)
            {
                JsonValue copy = item.first;
                if (set)
                {
                    if (!set->IsObject())
                    {
                        throw std::runtime_error("DSL variant.set must be an object");
                    }
                    for (const auto& patch : set->objectValue)
                    {
                        ApplyPatchPath(copy, patch.first, patch.second);
                    }
                }
                const std::string joined = item.second.empty() ? suffix : item.second + "_" + suffix;
                next.push_back({copy, joined});
            }
        }
        expanded = next.empty() ? expanded : next;
    }

    if (const auto sweeps = JsonFind(dslCase, "sweeps"))
    {
        if (!sweeps->IsArray())
        {
            throw std::runtime_error("DSL sweeps must be an array");
        }
        for (size_t sweepIndex = 0; sweepIndex < sweeps->arrayValue.size(); ++sweepIndex)
        {
            const auto& sweep = sweeps->arrayValue[sweepIndex];
            if (!sweep.IsObject())
            {
                throw std::runtime_error("DSL sweep must be an object");
            }
            const std::string path = DslStringOrDefault(sweep, "path", "");
            const auto values = JsonFind(sweep, "values");
            if (!values || !values->IsArray() || values->arrayValue.empty())
            {
                throw std::runtime_error("DSL sweep.values must be a non-empty array");
            }

            std::vector<std::pair<JsonValue, std::string>> next;
            for (const auto& item : expanded)
            {
                for (size_t valueIndex = 0; valueIndex < values->arrayValue.size(); ++valueIndex)
                {
                    const auto& raw = values->arrayValue[valueIndex];
                    JsonValue patchValue;
                    std::string suffix = "s" + std::to_string(sweepIndex + 1) + "_" + std::to_string(valueIndex + 1);
                    if (raw.IsObject())
                    {
                        suffix = SanitizeCaseId(DslStringOrDefault(raw, "suffix", suffix));
                        const auto value = JsonFind(raw, "value");
                        if (!value)
                        {
                            throw std::runtime_error("DSL sweep value object requires value");
                        }
                        patchValue = *value;
                    }
                    else
                    {
                        patchValue = raw;
                    }

                    JsonValue copy = item.first;
                    ApplyPatchPath(copy, path, patchValue);
                    const std::string joined = item.second.empty() ? suffix : item.second + "_" + suffix;
                    next.push_back({copy, joined});
                }
            }
            expanded = next;
        }
    }
    return expanded;
}

CaseRecipe CaseRecipeFromDslCase(
    const JsonValue& dslCase,
    const std::string& suffix,
    const JsonValue& defaults,
    const std::map<std::string, double>& symbols,
    const KeyPointMap& globalKeyPoints,
    const fs::path& sourcePath)
{
    if (!dslCase.IsObject())
    {
        throw std::runtime_error("DSL case must be an object");
    }

    CaseRecipe recipe;
    if (const auto value = symbols.find("max_model_size"); value != symbols.end())
    {
        recipe.maxModelSize = value->second;
        if (recipe.maxModelSize <= 0.0)
        {
            throw std::runtime_error("max_model_size must be > 0");
        }
    }
    const std::string baseId = SanitizeCaseId(DslStringOrDefault(dslCase, "case_id", DslStringOrDefault(dslCase, "id", "dsl_case")));
    KeyPointMap keyPoints = globalKeyPoints;
    if (const auto caseKeyPoints = JsonFind(dslCase, "key_points"))
    {
        AddDslKeyPoints(keyPoints, *caseKeyPoints, symbols, baseId + ".key_points");
    }
    if (defaults.IsObject())
    {
        ApplyDslOptions(recipe, defaults, symbols, keyPoints);
    }
    ApplyDslCaseOptions(recipe, dslCase, symbols, keyPoints);
    recipe.caseId = suffix.empty() ? baseId : baseId + "_" + suffix;
    recipe.dslSource = sourcePath.string();
    recipe.dslCaseId = baseId;
    recipe.dslVariant = suffix;
    recipe.hypothesis = DslStringOrDefault(dslCase, "hypothesis", "");
    recipe.sourceRef = DslStringFromCaseOrMetadata(dslCase, "source_ref");
    recipe.sourceTaskId = DslStringFromCaseOrMetadata(dslCase, "source_task_id");
    recipe.sourceTaskPath = DslStringFromCaseOrMetadata(dslCase, "source_task_path");
    recipe.sourceRiskId = DslStringFromCaseOrMetadata(dslCase, "source_risk_id");
    recipe.sourceRiskFamily = DslStringFromCaseOrMetadata(dslCase, "source_risk_family");
    recipe.sourceRiskCategories = DslStringFromCaseOrMetadata(dslCase, "source_risk_categories");

    if (recipe.api == "api_boolean")
    {
        const auto target = JsonFind(dslCase, "target");
        const auto tool = JsonFind(dslCase, "tool");
        if (!target || !tool)
        {
            throw std::runtime_error(recipe.caseId + " requires target and tool");
        }
        recipe.boolean.target = BodySpecFromJson(*target, symbols, recipe.caseId + ".target");
        recipe.boolean.tool = BodySpecFromJson(*tool, symbols, recipe.caseId + ".tool");
    }
    else if (recipe.api == "check_sgt" ||
             recipe.api == "step_import" ||
             recipe.api == "iges_import" ||
             recipe.api == "step_roundtrip" ||
             recipe.api == "iges_roundtrip")
    {
        const auto source = JsonFind(dslCase, "source_file");
        if (!source)
        {
            throw std::runtime_error(recipe.caseId + " requires source_file");
        }
        recipe.sourceFile = JsonString(*source, "source_file");
        if (const auto bodyIndex = JsonFind(dslCase, "body_index"))
        {
            recipe.sourceBodyIndex = JsonInteger(*bodyIndex, symbols, "body_index");
        }
        if (const auto sourceBodyIndex = JsonFind(dslCase, "source_body_index"))
        {
            recipe.sourceBodyIndex = JsonInteger(*sourceBodyIndex, symbols, "source_body_index");
        }
    }
    else
    {
        throw std::runtime_error("unsupported DSL api: " + recipe.api);
    }
    return recipe;
}

std::vector<CaseRecipe> LoadDslRecipes(const fs::path& path)
{
    const std::string json = ReadTextFile(path);
    JsonParser parser(json);
    const JsonValue root = parser.Parse();
    if (!root.IsObject())
    {
        throw std::runtime_error("DSL root must be an object");
    }
    const auto cases = JsonFind(root, "cases");
    if (!cases || !cases->IsArray() || cases->arrayValue.empty())
    {
        throw std::runtime_error("DSL cases must be a non-empty array");
    }

    const auto defaults = JsonFind(root, "defaults");
    const JsonValue emptyDefaults;
    const JsonValue& defaultsRef = defaults ? *defaults : emptyDefaults;
    const auto symbols = ResolveDslConstants(root);
    const auto globalKeyPoints = DslKeyPointsFromContainer(root, symbols, "root");

    std::vector<CaseRecipe> recipes;
    for (const auto& dslCase : cases->arrayValue)
    {
        for (const auto& expanded : ExpandDslCase(dslCase))
        {
            recipes.push_back(CaseRecipeFromDslCase(expanded.first, expanded.second, defaultsRef, symbols, globalKeyPoints, path));
        }
    }
    return recipes;
}

std::vector<CaseRecipe> LoadRecipes(const fs::path& path)
{
    if (path.empty())
    {
        return {CaseRecipe()};
    }
    const std::string json = ReadTextFile(path);
    if (json.find("\"dsl_version\"") != std::string::npos && json.find("\"cases\"") != std::string::npos)
    {
        return LoadDslRecipes(path);
    }
    return {LoadRecipe(path)};
}

CliOptions ParseCli(int argc, char** argv)
{
    CliOptions opts;
    for (int i = 1; i < argc; ++i)
    {
        const std::string arg = argv[i];
        auto needValue = [&](const std::string& name) -> std::string {
            if (i + 1 >= argc)
            {
                throw std::runtime_error("missing value for " + name);
            }
            return argv[++i];
        };

        if (arg == "--recipe")
        {
            opts.recipePath = needValue(arg);
        }
        else if (arg == "--out")
        {
            opts.outRoot = needValue(arg);
        }
        else if (arg == "--case-id")
        {
            opts.caseIdOverride = needValue(arg);
            RequireSafeCaseId(opts.caseIdOverride);
        }
        else if (arg == "--sdk-threads")
        {
            const std::string raw = needValue(arg);
            try
            {
                size_t consumed = 0;
                opts.sdkThreads = std::stoi(raw, &consumed);
                if (consumed != raw.size() || opts.sdkThreads < 1 || opts.sdkThreads > 64)
                {
                    throw std::runtime_error("range");
                }
            }
            catch (const std::exception&)
            {
                throw std::runtime_error("--sdk-threads must be an integer in [1, 64]");
            }
        }
        else if (arg == "--list-adapters-json")
        {
            opts.listAdaptersJson = true;
        }
        else if (arg == "--capture-flat-topotrack")
        {
            opts.captureFlatTopoTrack = true;
        }
        else if (arg == "--help" || arg == "-h")
        {
            std::cout << "Usage: sggk_case_runner [--recipe file.json] [--out artifacts] [--case-id id] [--sdk-threads 1] [--list-adapters-json] [--capture-flat-topotrack]\n";
            std::exit(0);
        }
        else
        {
            throw std::runtime_error("unknown argument: " + arg);
        }
    }
    return opts;
}

std::string TopoTypeName(sggk::TopoType type)
{
    switch (type)
    {
    case sggk::TopoType::Body: return "Body";
    case sggk::TopoType::Lump: return "Lump";
    case sggk::TopoType::Shell: return "Shell";
    case sggk::TopoType::SubShell: return "SubShell";
    case sggk::TopoType::Face: return "Face";
    case sggk::TopoType::Loop: return "Loop";
    case sggk::TopoType::Wire: return "Wire";
    case sggk::TopoType::Coedge: return "Coedge";
    case sggk::TopoType::Edge: return "Edge";
    case sggk::TopoType::Vertex: return "Vertex";
    }
    return "Unknown";
}

std::string TrackTypeName(sggk::TopoTrackType type)
{
    switch (type)
    {
    case sggk::TopoTrackType::Undefined: return "Undefined";
    case sggk::TopoTrackType::Delete: return "Delete";
    case sggk::TopoTrackType::Merge: return "Merge";
    case sggk::TopoTrackType::Create: return "Create";
    case sggk::TopoTrackType::Split: return "Split";
    case sggk::TopoTrackType::Derive: return "Derive";
    }
    return "Unknown";
}

sggk::BooleanType ParseBooleanType(const std::string& type)
{
    if (type == "UNION")
    {
        return sggk::BooleanType::UNION;
    }
    if (type == "INTERSECTION")
    {
        return sggk::BooleanType::INTERSECTION;
    }
    if (type == "SUBTRACTION")
    {
        return sggk::BooleanType::SUBTRACTION;
    }
    throw std::runtime_error("unsupported boolean_type: " + type);
}

std::string UpperToken(std::string value)
{
    std::transform(value.begin(), value.end(), value.begin(), [](unsigned char ch) {
        return static_cast<char>(std::toupper(ch));
    });
    return value;
}

sggk::StepAppProtocol ParseStepAppProtocol(const std::string& protocol)
{
    const std::string token = UpperToken(protocol);
    if (token == "AP203")
    {
        return sggk::StepAppProtocol::AP203;
    }
    if (token == "AP214")
    {
        return sggk::StepAppProtocol::AP214;
    }
    if (token == "AP242")
    {
        return sggk::StepAppProtocol::AP242;
    }
    throw std::runtime_error("unsupported step_app_protocol: " + protocol);
}

void RequirePositive(double value, const std::string& name)
{
    if (value <= 0.0)
    {
        throw std::runtime_error(name + " must be > 0");
    }
}

std::string StringArrayJson(const std::vector<std::string>& values)
{
    std::ostringstream os;
    os << "[";
    for (size_t i = 0; i < values.size(); ++i)
    {
        if (i != 0)
        {
            os << ",";
        }
        os << "\"" << EscapeJson(values[i]) << "\"";
    }
    os << "]";
    return os.str();
}

std::string NumericExpectationJson(const NumericExpectation& expectation)
{
    std::ostringstream os;
    os << "{"
       << "\"min_set\":" << (expectation.minSet ? "true" : "false")
       << ",\"min\":" << std::setprecision(17) << expectation.minValue
       << ",\"max_set\":" << (expectation.maxSet ? "true" : "false")
       << ",\"max\":" << std::setprecision(17) << expectation.maxValue
       << ",\"expected_set\":" << (expectation.expectedSet ? "true" : "false")
       << ",\"expected\":" << std::setprecision(17) << expectation.expectedValue
       << ",\"abs_tol\":" << std::setprecision(17) << expectation.absTol
       << ",\"rel_tol\":" << std::setprecision(17) << expectation.relTol
       << "}";
    return os.str();
}

std::string PointRelationExpectationJson(const PointRelationExpectation& relation)
{
    std::ostringstream os;
    os << "{"
       << "\"id\":\"" << EscapeJson(relation.id) << "\""
       << ",\"role\":\"" << EscapeJson(relation.role) << "\""
       << ",\"body_index\":" << relation.bodyIndex
       << ",\"point_ref\":\"" << EscapeJson(relation.pointRef) << "\""
       << ",\"point\":[" << std::setprecision(17) << relation.x
       << "," << std::setprecision(17) << relation.y
       << "," << std::setprecision(17) << relation.z << "]"
       << ",\"expected\":\"" << EscapeJson(relation.expected) << "\""
       << ",\"tolerance\":" << std::setprecision(17) << relation.tolerance
       << ",\"check_boundary\":" << (relation.checkBoundary ? "true" : "false")
       << ",\"required\":" << (relation.required ? "true" : "false")
       << "}";
    return os.str();
}

std::string PointRelationExpectationsJson(const std::vector<PointRelationExpectation>& relations)
{
    std::ostringstream os;
    os << "[";
    for (size_t i = 0; i < relations.size(); ++i)
    {
        if (i != 0)
        {
            os << ",";
        }
        os << PointRelationExpectationJson(relations[i]);
    }
    os << "]";
    return os.str();
}

std::string FacePointRelationExpectationJson(const FacePointRelationExpectation& relation)
{
    std::ostringstream os;
    os << "{"
       << "\"id\":\"" << EscapeJson(relation.id) << "\""
       << ",\"role\":\"" << EscapeJson(relation.role) << "\""
       << ",\"body_index\":" << relation.bodyIndex
       << ",\"face_index\":" << relation.faceIndex
       << ",\"face_id_set\":" << (relation.useFaceId ? "true" : "false")
       << ",\"face_id\":" << relation.faceId
       << ",\"expected\":\"" << EscapeJson(relation.expected) << "\""
       << ",\"tolerance\":" << std::setprecision(17) << relation.tolerance
       << ",\"check_boundary\":" << (relation.checkBoundary ? "true" : "false")
       << ",\"required\":" << (relation.required ? "true" : "false")
       << ",\"has_point\":" << (relation.hasPoint ? "true" : "false")
       << ",\"point_ref\":\"" << EscapeJson(relation.pointRef) << "\""
       << ",\"point\":[" << std::setprecision(17) << relation.x
       << "," << std::setprecision(17) << relation.y
       << "," << std::setprecision(17) << relation.z << "]"
       << ",\"has_uv\":" << (relation.hasUv ? "true" : "false")
       << ",\"uv\":[" << std::setprecision(17) << relation.u
       << "," << std::setprecision(17) << relation.v << "]"
       << ",\"has_uv_fraction\":" << (relation.hasUvFraction ? "true" : "false")
       << ",\"uv_fraction\":[" << std::setprecision(17) << relation.uFraction
       << "," << std::setprecision(17) << relation.vFraction << "]"
       << "}";
    return os.str();
}

std::string FacePointRelationExpectationsJson(const std::vector<FacePointRelationExpectation>& relations)
{
    std::ostringstream os;
    os << "[";
    for (size_t i = 0; i < relations.size(); ++i)
    {
        if (i != 0)
        {
            os << ",";
        }
        os << FacePointRelationExpectationJson(relations[i]);
    }
    os << "]";
    return os.str();
}

std::string ClashExpectationJson(const ClashExpectation& check)
{
    std::ostringstream os;
    os << "{"
       << "\"id\":\"" << EscapeJson(check.id) << "\""
       << ",\"role_a\":\"" << EscapeJson(check.roleA) << "\""
       << ",\"role_b\":\"" << EscapeJson(check.roleB) << "\""
       << ",\"body_index_a\":" << check.bodyIndexA
       << ",\"body_index_b\":" << check.bodyIndexB
       << ",\"expected\":\"" << EscapeJson(check.expected) << "\""
       << ",\"mode\":\"" << EscapeJson(check.mode) << "\""
       << ",\"tolerance\":" << std::setprecision(17) << check.tolerance
       << ",\"required\":" << (check.required ? "true" : "false")
       << "}";
    return os.str();
}

std::string ClashExpectationsJson(const std::vector<ClashExpectation>& checks)
{
    std::ostringstream os;
    os << "[";
    for (size_t i = 0; i < checks.size(); ++i)
    {
        if (i != 0)
        {
            os << ",";
        }
        os << ClashExpectationJson(checks[i]);
    }
    os << "]";
    return os.str();
}

std::string DistanceExpectationJson(const DistanceExpectation& check)
{
    std::ostringstream os;
    os << "{"
       << "\"id\":\"" << EscapeJson(check.id) << "\""
       << ",\"role_a\":\"" << EscapeJson(check.roleA) << "\""
       << ",\"role_b\":\"" << EscapeJson(check.roleB) << "\""
       << ",\"body_index_a\":" << check.bodyIndexA
       << ",\"body_index_b\":" << check.bodyIndexB
       << ",\"kind\":\"" << EscapeJson(check.kind) << "\""
       << ",\"threshold\":" << std::setprecision(17) << check.threshold
       << ",\"distance\":" << NumericExpectationJson(check.distance)
       << ",\"required\":" << (check.required ? "true" : "false")
       << "}";
    return os.str();
}

std::string DistanceExpectationsJson(const std::vector<DistanceExpectation>& checks)
{
    std::ostringstream os;
    os << "[";
    for (size_t i = 0; i < checks.size(); ++i)
    {
        if (i != 0)
        {
            os << ",";
        }
        os << DistanceExpectationJson(checks[i]);
    }
    os << "]";
    return os.str();
}

std::string PlaneExtremeExpectationJson(const PlaneExtremeExpectation& check)
{
    std::ostringstream os;
    os << "{"
       << "\"id\":\"" << EscapeJson(check.id) << "\""
       << ",\"role\":\"" << EscapeJson(check.role) << "\""
       << ",\"body_index\":" << check.bodyIndex
       << ",\"axis\":\"" << EscapeJson(check.axis) << "\""
       << ",\"side\":\"" << EscapeJson(check.side) << "\""
       << ",\"expected\":";
    if (check.expectedSet)
    {
        os << std::setprecision(17) << check.expected;
    }
    else
    {
        os << "null";
    }
    os << ",\"compare_expected\":" << (check.compareExpected ? "true" : "false")
       << ",\"tolerance\":" << std::setprecision(17) << check.tolerance
       << ",\"probe_coordinate_set\":" << (check.probeCoordinateSet ? "true" : "false")
       << ",\"probe_coordinate\":" << std::setprecision(17) << check.probeCoordinate
       << ",\"plane_span\":" << std::setprecision(17) << check.planeSpan
       << ",\"plane_span_scale\":" << std::setprecision(17) << check.planeSpanScale
       << ",\"required\":" << (check.required ? "true" : "false")
       << ",\"export_debug_geometry\":" << (check.exportDebugGeometry ? "true" : "false")
       << "}";
    return os.str();
}

std::string PlaneExtremeExpectationsJson(const std::vector<PlaneExtremeExpectation>& checks)
{
    std::ostringstream os;
    os << "[";
    for (size_t i = 0; i < checks.size(); ++i)
    {
        if (i != 0)
        {
            os << ",";
        }
        os << PlaneExtremeExpectationJson(checks[i]);
    }
    os << "]";
    return os.str();
}

std::string ValidationExpectationsJson(const ValidationExpectations& expectations)
{
    std::ostringstream os;
    os << "{"
       << "\"min_result_bodies\":" << expectations.minResultBodies
       << ",\"max_result_bodies_set\":" << (expectations.maxResultBodiesSet ? "true" : "false")
       << ",\"max_result_bodies\":" << expectations.maxResultBodies
       << ",\"require_property_calculations\":" << (expectations.requirePropertyCalculations ? "true" : "false")
       << ",\"require_finite_properties\":" << (expectations.requireFiniteProperties ? "true" : "false")
       << ",\"require_nonnegative_length_area\":" << (expectations.requireNonnegativeLengthArea ? "true" : "false")
       << ",\"require_nonnegative_volume\":" << (expectations.requireNonnegativeVolume ? "true" : "false")
       << ",\"boolean_volume_relation\":" << (expectations.booleanVolumeRelation ? "true" : "false")
       << ",\"boolean_bbox_relation\":" << (expectations.booleanBboxRelation ? "true" : "false")
       << ",\"sample_input_properties\":" << (expectations.sampleInputProperties ? "true" : "false")
       << ",\"volume_relation_abs_tol\":" << std::setprecision(17) << expectations.relationAbsTol
       << ",\"volume_relation_rel_tol\":" << std::setprecision(17) << expectations.relationRelTol
       << ",\"total_length\":" << NumericExpectationJson(expectations.totalLength)
       << ",\"total_area\":" << NumericExpectationJson(expectations.totalArea)
       << ",\"total_volume\":" << NumericExpectationJson(expectations.totalVolume)
       << ",\"total_abs_volume\":" << NumericExpectationJson(expectations.totalAbsVolume)
       << ",\"point_relations\":" << PointRelationExpectationsJson(expectations.pointRelations)
       << ",\"face_point_relations\":" << FacePointRelationExpectationsJson(expectations.facePointRelations)
       << ",\"clash_checks\":" << ClashExpectationsJson(expectations.clashChecks)
       << ",\"distance_checks\":" << DistanceExpectationsJson(expectations.distanceChecks)
       << ",\"plane_extreme_checks\":" << PlaneExtremeExpectationsJson(expectations.planeExtremeChecks)
       << "}";
    return os.str();
}

std::string LastStringOrEmpty(const std::vector<std::string>& values)
{
    return values.empty() ? std::string() : values.back();
}

std::string PointJson(const sggk::Point3D& point)
{
    std::ostringstream os;
    os << "[" << std::setprecision(17) << point.X()
       << "," << std::setprecision(17) << point.Y()
       << "," << std::setprecision(17) << point.Z() << "]";
    return os.str();
}

std::string BndBoxObjectJson(const sggk::BndBox& box)
{
    std::ostringstream os;
    os << "{"
       << "\"empty\":" << (box.IsEmpty() ? "true" : "false");
    if (!box.IsEmpty())
    {
        os << ",\"min\":" << PointJson(box.MinPoint())
           << ",\"max\":" << PointJson(box.MaxPoint())
           << ",\"center\":" << PointJson(box.Center())
           << ",\"diagonal\":" << std::setprecision(17) << box.Length();
    }
    os << "}";
    return os.str();
}

std::string BndBoxJson(const sggk::TopologyPtr& topology)
{
    if (!topology)
    {
        return "null";
    }

    try
    {
        const auto box = topology->CalcBndBox(true);
        return BndBoxObjectJson(box);
    }
    catch (const std::exception& ex)
    {
        return "{\"error\":\"" + EscapeJson(ex.what()) + "\"}";
    }
    catch (...)
    {
        return "{\"error\":\"unknown\"}";
    }
}

std::string TopologyKey(const std::string& role, const std::string& type, sggk::ID id)
{
    return role + "|" + type + "|" + std::to_string(id);
}

std::string TopologyKey(const std::string& type, sggk::ID id)
{
    return type + "|" + std::to_string(id);
}

void AddTopologyRef(InputTopologyIndex& index, TopologyRef ref)
{
    if (!ref.topology)
    {
        return;
    }

    const size_t entryIndex = index.entries.size();
    index.byPtr[ref.topology.get()] = entryIndex;
    index.byRoleTypeId[TopologyKey(ref.role, ref.type, ref.id)].push_back(entryIndex);
    index.byTypeId[TopologyKey(ref.type, ref.id)].push_back(entryIndex);
    index.entries.push_back(std::move(ref));
}

template <typename TopologyList>
void AddTopologyListRefs(
    InputTopologyIndex& index,
    const std::string& role,
    const sggk::BodyPtr& body,
    const std::vector<std::string>& operations,
    const TopologyList& topologies)
{
    int localIndex = 0;
    for (const auto& item : topologies)
    {
        sggk::TopologyPtr topology = item;
        if (!topology)
        {
            continue;
        }

        const auto owner = topology->OwnerBody();
        TopologyRef ref;
        ref.role = role;
        ref.type = TopoTypeName(topology->TopoType());
        ref.id = topology->ID();
        ref.localIndex = localIndex++;
        ref.bodyId = owner ? owner->ID() : (body ? body->ID() : 0);
        ref.operations = operations;
        ref.topology = topology;
        AddTopologyRef(index, std::move(ref));
    }
}

void AddBodyRefs(
    InputTopologyIndex& index,
    const std::string& role,
    const sggk::BodyPtr& body,
    const std::vector<std::string>& operations)
{
    if (!body)
    {
        return;
    }

    index.roleByBodyPtr[body.get()] = role;

    TopologyRef bodyRef;
    bodyRef.role = role;
    bodyRef.type = TopoTypeName(body->TopoType());
    bodyRef.id = body->ID();
    bodyRef.localIndex = 0;
    bodyRef.bodyId = body->ID();
    bodyRef.operations = operations;
    bodyRef.topology = body;
    AddTopologyRef(index, std::move(bodyRef));

    AddTopologyListRefs(index, role, body, operations, body->Lumps());
    AddTopologyListRefs(index, role, body, operations, body->QueryShells());
    AddTopologyListRefs(index, role, body, operations, body->QueryFaces());
    AddTopologyListRefs(index, role, body, operations, body->QueryWires());
    AddTopologyListRefs(index, role, body, operations, body->QueryEdges());
    AddTopologyListRefs(index, role, body, operations, body->QueryVertices());
}

InputTopologyIndex BuildInputTopologyIndex(
    const CaseRecipe& recipe,
    const sggk::BodyPtr& target,
    const sggk::BodyPtr& tool)
{
    InputTopologyIndex index;
    AddBodyRefs(index, "target", target, recipe.boolean.target.operations);
    AddBodyRefs(index, "tool", tool, recipe.boolean.tool.operations);
    return index;
}

const TopologyRef* FindInputTopologyRef(
    const InputTopologyIndex& index,
    const sggk::TopologyPtr& topology,
    bool& ambiguous)
{
    ambiguous = false;
    if (!topology)
    {
        return nullptr;
    }

    const auto ptrIt = index.byPtr.find(topology.get());
    if (ptrIt != index.byPtr.end())
    {
        return &index.entries[ptrIt->second];
    }

    const std::string type = TopoTypeName(topology->TopoType());
    const auto owner = topology->OwnerBody();
    if (owner)
    {
        const auto roleIt = index.roleByBodyPtr.find(owner.get());
        if (roleIt != index.roleByBodyPtr.end())
        {
            const auto keyIt = index.byRoleTypeId.find(TopologyKey(roleIt->second, type, topology->ID()));
            if (keyIt != index.byRoleTypeId.end())
            {
                if (keyIt->second.size() == 1)
                {
                    return &index.entries[keyIt->second.front()];
                }
                ambiguous = true;
                return nullptr;
            }
        }
    }

    const auto keyIt = index.byTypeId.find(TopologyKey(type, topology->ID()));
    if (keyIt != index.byTypeId.end())
    {
        if (keyIt->second.size() == 1)
        {
            return &index.entries[keyIt->second.front()];
        }
        ambiguous = true;
    }
    return nullptr;
}

std::string InputRefJson(const TopologyRef& ref)
{
    std::ostringstream os;
    os << "{"
       << "\"role\":\"" << EscapeJson(ref.role) << "\""
       << ",\"body_id\":" << ref.bodyId
       << ",\"type\":\"" << EscapeJson(ref.type) << "\""
       << ",\"id\":" << ref.id
       << ",\"local_index\":" << ref.localIndex
       << ",\"terminal_operation\":\"" << EscapeJson(LastStringOrEmpty(ref.operations)) << "\""
       << ",\"operation_chain\":" << StringArrayJson(ref.operations)
       << "}";
    return os.str();
}

std::string TopologyLocatorJson(const sggk::TopologyPtr& topology)
{
    if (!topology)
    {
        return "null";
    }

    if (const auto vertex = std::dynamic_pointer_cast<sggk::Vertex>(topology))
    {
        try
        {
            std::ostringstream os;
            os << "{"
               << "\"point\":" << PointJson(vertex->GeomPoint())
               << ",\"tolerance\":" << std::setprecision(17) << vertex->Tolerance()
               << "}";
            return os.str();
        }
        catch (const std::exception& ex)
        {
            return "{\"error\":\"" + EscapeJson(ex.what()) + "\"}";
        }
    }

    if (const auto edge = std::dynamic_pointer_cast<sggk::Edge>(topology))
    {
        std::ostringstream os;
        os << "{"
           << "\"bbox\":" << BndBoxJson(topology)
           << ",\"tolerance\":" << std::setprecision(17) << edge->Tolerance();
        try
        {
            const auto startPoint = edge->StartPoint();
            const auto endPoint = edge->EndPoint();
            const auto length = edge->CalcLength();
            os << ",\"start_point\":" << PointJson(startPoint)
               << ",\"end_point\":" << PointJson(endPoint)
               << ",\"length\":" << std::setprecision(17) << length;
        }
        catch (const std::exception& ex)
        {
            os << ",\"edge_error\":\"" << EscapeJson(ex.what()) << "\"";
        }
        os << "}";
        return os.str();
    }

    if (const auto face = std::dynamic_pointer_cast<sggk::Face>(topology))
    {
        std::ostringstream os;
        os << "{"
           << "\"bbox\":" << BndBoxJson(topology)
           << ",\"sense\":" << (face->Sense() ? "true" : "false");
        try
        {
            const auto area = face->CalcArea();
            os << ",\"area\":" << std::setprecision(17) << area;
        }
        catch (const std::exception& ex)
        {
            os << ",\"face_error\":\"" << EscapeJson(ex.what()) << "\"";
        }
        os << "}";
        return os.str();
    }

    return "{\"bbox\":" + BndBoxJson(topology) + "}";
}

std::string TopologyEntityJson(const sggk::TopologyPtr& topology)
{
    if (!topology)
    {
        return "null";
    }

    std::ostringstream os;
    os << "{"
       << "\"id\":" << topology->ID()
       << ",\"type\":\"" << TopoTypeName(topology->TopoType()) << "\""
       << ",\"locator\":" << TopologyLocatorJson(topology)
       << "}";
    return os.str();
}

std::string BodySpecJson(const BodySpec& spec)
{
    std::ostringstream os;
    os << "{"
       << "\"kind\":\"" << EscapeJson(spec.kind) << "\""
       << ",\"boolean_type\":\"" << EscapeJson(spec.booleanType) << "\""
       << ",\"radius\":" << std::setprecision(17) << spec.radius
       << ",\"height\":" << std::setprecision(17) << spec.height
       << ",\"angle\":" << std::setprecision(17) << spec.angle
       << ",\"create_seam_edge\":" << (spec.createSeamEdge ? "true" : "false")
       << ",\"length\":" << std::setprecision(17) << spec.length
       << ",\"width\":" << std::setprecision(17) << spec.width
       << ",\"bottom_radius\":" << std::setprecision(17) << spec.bottomRadius
       << ",\"top_radius\":" << std::setprecision(17) << spec.topRadius
       << ",\"inner_radius\":" << std::setprecision(17) << spec.innerRadius
       << ",\"outer_radius\":" << std::setprecision(17) << spec.outerRadius
       << ",\"long_radius\":" << std::setprecision(17) << spec.longRadius
       << ",\"short_radius\":" << std::setprecision(17) << spec.shortRadius
       << ",\"profile_radius\":" << std::setprecision(17) << spec.profileRadius
       << ",\"path_radius\":" << std::setprecision(17) << spec.pathRadius
       << ",\"secondary_height\":" << std::setprecision(17) << spec.secondaryHeight
       << ",\"secondary_translate\":[" << std::setprecision(17) << spec.secondaryTranslateX
       << "," << std::setprecision(17) << spec.secondaryTranslateY
       << "," << std::setprecision(17) << spec.secondaryTranslateZ << "]"
       << ",\"min_dist\":" << std::setprecision(17) << spec.minDist
       << ",\"max_dist\":" << std::setprecision(17) << spec.maxDist
       << ",\"operation_tol\":" << std::setprecision(17) << spec.operationTol
       << ",\"g1_tol\":" << std::setprecision(17) << spec.g1Tol
       << ",\"allow_partial_success\":" << (spec.allowPartialSuccess ? "true" : "false")
       << ",\"translate\":[" << std::setprecision(17) << spec.translateX
       << "," << std::setprecision(17) << spec.translateY
       << "," << std::setprecision(17) << spec.translateZ << "]"
       << ",\"scale\":" << std::setprecision(17) << spec.scale
       << ",\"source_file\":\"" << EscapeJson(spec.sourceFile.string()) << "\""
       << ",\"body_index\":" << spec.bodyIndex
       << ",\"operations\":" << StringArrayJson(spec.operations)
       << "}";
    return os.str();
}

void ApplyBodyTransform(const BodySpec& spec, const sggk::BodyPtr& body)
{
    if (!body)
    {
        return;
    }

    if (spec.scale <= 0.0)
    {
        throw std::runtime_error("primitive scale must be > 0");
    }
    if (std::fabs(spec.scale - 1.0) > 1e-15)
    {
        body->Transform(sggk::Matrix4::MakeScale(spec.scale));
    }
    if (std::fabs(spec.translateX) > 0.0 || std::fabs(spec.translateY) > 0.0 || std::fabs(spec.translateZ) > 0.0)
    {
        body->Transform(sggk::Matrix4::MakeTranslation(spec.translateX, spec.translateY, spec.translateZ));
    }
}

sggk::BodyPtr FirstResultBody(const sggk::ModelingRetPtr& ret, const std::string& role)
{
    if (!ret || !ret->Succeeded())
    {
        const std::string msg = ret ? ret->Status().ErrorMsg() : "null modeling return";
        throw std::runtime_error(role + " construction failed: " + msg);
    }
    if (ret->ResultBodies().empty())
    {
        throw std::runtime_error(role + " construction produced no result bodies");
    }
    auto body = ret->ResultBodies().front();
    if (!body)
    {
        throw std::runtime_error(role + " construction produced null body");
    }
    return body;
}

bool IsPrimitiveKind(const std::string& kind)
{
    return kind == "solid_cylinder" ||
           kind == "solid_wedge" ||
           kind == "solid_sphere" ||
           kind == "solid_cone" ||
           kind == "solid_torus";
}

sggk::BodyPtr MakePlaneSheetBody(const BodySpec& spec, const std::string& role)
{
    RequirePositive(spec.length, role + "_length");
    RequirePositive(spec.width, role + "_width");
    auto plane = std::make_shared<sggk::Plane>(
        sggk::Point3D(0.0, 0.0, 0.0),
        sggk::Dir3D(0.0, 0.0, 1.0));
    auto face = sggk::api_create_face(
        plane,
        sggk::UVRange(
            sggk::Interval(-0.5 * spec.length, 0.5 * spec.length),
            sggk::Interval(-0.5 * spec.width, 0.5 * spec.width)));
    auto body = sggk::api_topo_to_body(face);
    if (!body)
    {
        throw std::runtime_error(role + " plane_sheet failed to create body");
    }
    ApplyBodyTransform(spec, body);
    return body;
}

std::vector<sggk::BodyPtr> LoadSgtBodiesFromFile(const fs::path& sourceFile, const std::string& role)
{
    if (sourceFile.empty())
    {
        throw std::runtime_error(role + " requires source_file");
    }

    sggk::RapidTopoJsonDeserializer deserializer;
    std::vector<sggk::BodyPtr> bodies;
    auto loadedBodies = deserializer.DeserializeBodiesFromFile(sourceFile.string().c_str());
    for (const auto& body : loadedBodies)
    {
        bodies.push_back(body);
    }
    if (bodies.empty())
    {
        auto body = deserializer.DeserializeBodyFromFile(sourceFile.string().c_str());
        if (body)
        {
            bodies.push_back(body);
        }
    }
    if (bodies.empty())
    {
        throw std::runtime_error(role + " produced no bodies: " + sourceFile.string());
    }
    return bodies;
}

sggk::BodyPtr SelectSgtBody(const fs::path& sourceFile, int bodyIndex, const std::string& role)
{
    if (bodyIndex < 0)
    {
        throw std::runtime_error(role + " body_index must be >= 0");
    }

    auto bodies = LoadSgtBodiesFromFile(sourceFile, role);
    if (static_cast<size_t>(bodyIndex) >= bodies.size())
    {
        throw std::runtime_error(role + " body_index out of range");
    }
    auto body = bodies[static_cast<size_t>(bodyIndex)];
    if (!body)
    {
        throw std::runtime_error(role + " selected null body");
    }
    return body;
}

sggk::BodyPtr MakeLoadedSgtBody(const BodySpec& spec, const std::string& role)
{
    if (spec.sourceFile.empty())
    {
        throw std::runtime_error(role + " loaded_sgt requires source_file");
    }
    auto body = SelectSgtBody(spec.sourceFile, spec.bodyIndex, role + " loaded_sgt");
    ApplyBodyTransform(spec, body);
    return body;
}

sggk::BodyPtr MakePrimitiveBody(const BodySpec& spec, const std::string& role)
{
    sggk::PrimitivesRetPtr ret;
    if (spec.kind == "solid_cylinder")
    {
        RequirePositive(spec.radius, role + "_radius");
        RequirePositive(spec.height, role + "_height");
        RequirePositive(spec.angle, role + "_angle");
        ret = sggk::api_make_solid_cylinder(
            sggk::Ucs3D(),
            spec.radius,
            spec.height,
            spec.angle,
            spec.createSeamEdge);
    }
    else if (spec.kind == "solid_wedge")
    {
        RequirePositive(spec.length, role + "_length");
        RequirePositive(spec.width, role + "_width");
        RequirePositive(spec.height, role + "_height");
        ret = sggk::api_make_solid_wedge(
            sggk::Ucs3D(),
            spec.length,
            spec.width,
            spec.height);
    }
    else if (spec.kind == "solid_sphere")
    {
        RequirePositive(spec.radius, role + "_radius");
        ret = sggk::api_make_solid_sphere(
            sggk::Ucs3D(),
            spec.radius,
            spec.createSeamEdge);
    }
    else if (spec.kind == "solid_cone")
    {
        RequirePositive(spec.bottomRadius, role + "_bottom_radius");
        RequirePositive(spec.height, role + "_height");
        RequirePositive(spec.angle, role + "_angle");
        ret = sggk::api_make_solid_cone(
            sggk::Ucs3D(),
            spec.bottomRadius,
            spec.topRadius,
            spec.height,
            spec.angle,
            spec.createSeamEdge);
    }
    else if (spec.kind == "solid_torus")
    {
        RequirePositive(spec.longRadius, role + "_long_radius");
        RequirePositive(spec.shortRadius, role + "_short_radius");
        RequirePositive(spec.angle, role + "_angle");
        ret = sggk::api_make_solid_torus(
            sggk::Ucs3D(),
            spec.longRadius,
            spec.shortRadius,
            spec.angle,
            spec.createSeamEdge);
    }
    else
    {
        throw std::runtime_error("unsupported " + role + "_kind: " + spec.kind);
    }

    if (!ret || !ret->Succeeded())
    {
        const std::string msg = ret ? ret->Status().ErrorMsg() : "null primitive return";
        throw std::runtime_error(role + " construction failed: " + msg);
    }
    auto body = ret->ResultBody();
    if (!body)
    {
        throw std::runtime_error(role + " construction produced null body");
    }
    ApplyBodyTransform(spec, body);
    return body;
}

sggk::BodyPtr MakeExtrudedRectBody(const BodySpec& spec, const std::string& role)
{
    RequirePositive(spec.length, role + "_length");
    RequirePositive(spec.width, role + "_width");
    RequirePositive(spec.height, role + "_height");

    const sggk::Plane plane;
    const sggk::BndBox2D box(
        sggk::Point2D(-0.5 * spec.length, -0.5 * spec.width),
        sggk::Point2D(0.5 * spec.length, 0.5 * spec.width));
    auto sheet = sggk::api_create_rect_sheet_body(plane, box);
    if (!sheet)
    {
        throw std::runtime_error(role + " extrude_rect failed to create sheet body");
    }

    auto ret = sggk::api_extrude_entity(sheet, sggk::Dir3D::UnitZ, 0.0, spec.height, true);
    auto body = FirstResultBody(ret, role + " extrude_rect");
    ApplyBodyTransform(spec, body);
    return body;
}

sggk::BodyPtr MakeThickenedRectSheetBody(const BodySpec& spec, const std::string& role)
{
    RequirePositive(spec.length, role + "_length");
    RequirePositive(spec.width, role + "_width");
    RequirePositive(spec.operationTol, role + "_operation_tol");
    RequirePositive(spec.g1Tol, role + "_g1_tol");
    if (!(spec.maxDist > spec.minDist))
    {
        throw std::runtime_error(role + "_max_dist must be greater than " + role + "_min_dist");
    }

    const sggk::Plane plane;
    const sggk::BndBox2D box(
        sggk::Point2D(-0.5 * spec.length, -0.5 * spec.width),
        sggk::Point2D(0.5 * spec.length, 0.5 * spec.width));
    auto sheet = sggk::api_create_rect_sheet_body(plane, box);
    if (!sheet)
    {
        throw std::runtime_error(role + " thicken_rect_sheet failed to create sheet body");
    }

    sggk::ThickenOpts opts;
    opts.SetModelingTol(spec.operationTol);
    opts.SetCheckValid(true);
    opts.SetToTopoTrack(false);
    opts.SetNearTangentAngle(spec.g1Tol);
    opts.SetAllowPartialSuccess(spec.allowPartialSuccess);

    auto ret = sggk::api_thicken_body(sheet, spec.minDist, spec.maxDist, opts);
    auto body = FirstResultBody(ret, role + " thicken_rect_sheet");
    ApplyBodyTransform(spec, body);
    return body;
}

sggk::BodyPtr MakeSweepCircleLineBody(const BodySpec& spec, const std::string& role)
{
    RequirePositive(spec.profileRadius, role + "_profile_radius");
    RequirePositive(spec.height, role + "_height");
    RequirePositive(spec.operationTol, role + "_operation_tol");
    RequirePositive(spec.g1Tol, role + "_g1_tol");

    sggk::Circle3D circle(sggk::Ucs3D(), spec.profileRadius);
    auto profileEdge = sggk::TopoBuilder::MakeEdge(circle);
    auto profileCoedge = sggk::TopoBuilder::MakeCoedge(profileEdge, true);
    auto profileWire = sggk::TopoBuilder::MakeWire({ profileCoedge }, sggk::WireType::Closed);

    auto startVertex = sggk::TopoBuilder::MakeVertex(sggk::Point3D(0.0, 0.0, 0.0));
    auto endVertex = sggk::TopoBuilder::MakeVertex(sggk::Point3D(0.0, 0.0, spec.height));
    auto pathCoedge = sggk::TopoBuilder::MakeCoedge(
        sggk::TopoBuilder::MakeLinearEdge(startVertex, endVertex),
        true);
    auto pathWire = sggk::TopoBuilder::MakeWire({ pathCoedge }, sggk::WireType::Open);

    sggk::SweepOpts opts;
    opts.SetSweepMode(sggk::SweepMode::Normal);
    opts.SetG1Tol(spec.g1Tol);
    opts.SetModelingTol(spec.operationTol);
    opts.SetRelocateProfile(true);

    auto ret = sggk::api_sweep_entity(profileWire, pathWire, opts);
    auto body = FirstResultBody(ret, role + " sweep_circle_line");
    ApplyBodyTransform(spec, body);
    return body;
}

sggk::BodyPtr MakeSupportSweepBSplineSurfaceBody(const BodySpec& spec, const std::string& role)
{
    RequirePositive(spec.pathRadius, role + "_path_radius");
    RequirePositive(spec.profileRadius, role + "_profile_radius");
    RequirePositive(spec.height, role + "_height");
    RequirePositive(spec.operationTol, role + "_operation_tol");
    RequirePositive(spec.g1Tol, role + "_g1_tol");

    const double xyScale = spec.pathRadius / 45.53;
    const double zScale = spec.height / 20.0;
    auto point = [xyScale, zScale](double x, double y, double z) {
        return sggk::Point3D(x * xyScale, y * xyScale, z * zScale);
    };

    sggk::Point3DMatrix ctrlPoints(6);
    ctrlPoints[0].push_back(point(-45.53, -0.52, 0.00));
    ctrlPoints[0].push_back(point(-45.53, -0.52, 20.00));
    ctrlPoints[1].push_back(point(-34.78, 8.52, 0.00));
    ctrlPoints[1].push_back(point(-34.78, 8.52, 20.00));
    ctrlPoints[2].push_back(point(-11.68, 21.38, 0.00));
    ctrlPoints[2].push_back(point(-11.68, 21.38, 20.00));
    ctrlPoints[3].push_back(point(14.93, -1.33, 0.00));
    ctrlPoints[3].push_back(point(14.93, -1.33, 20.00));
    ctrlPoints[4].push_back(point(32.74, 0.76, 0.00));
    ctrlPoints[4].push_back(point(32.74, 0.76, 20.00));
    ctrlPoints[5].push_back(point(41.49, 2.54, 0.00));
    ctrlPoints[5].push_back(point(41.49, 2.54, 20.00));

    sggk::RealArray knotU{0, 0.42, 0.73, 1};
    sggk::UIntArray multU{4, 1, 1, 4};
    sggk::RealArray knotV{0, 1};
    sggk::UIntArray multV{2, 2};

    sggk::BSplineSurfacePtr supportSurface(
        new sggk::BSplineSurface(3, 1, ctrlPoints, knotU, knotV, multU, multV));
    auto supportFace = sggk::api_create_face(
        supportSurface,
        sggk::UVRange(supportSurface->DomainU(), supportSurface->DomainV()));
    if (!supportFace)
    {
        throw std::runtime_error(role + " support_sweep_bspline_surface failed to create support face");
    }
    auto supportBody = sggk::TopoBuilder::MakeBody(supportFace);
    if (!supportBody)
    {
        throw std::runtime_error(role + " support_sweep_bspline_surface failed to create support body");
    }

    const auto pathCurve = std::dynamic_pointer_cast<sggk::BoundedCurve3D>(supportSurface->CalcUCurve(0.5));
    if (!pathCurve)
    {
        throw std::runtime_error(role + " support_sweep_bspline_surface failed to derive support path");
    }
    auto pathEdge = sggk::TopoBuilder::MakeEdge(*pathCurve);
    auto pathCoedge = sggk::TopoBuilder::MakeCoedge(pathEdge, true);
    auto pathWire = sggk::TopoBuilder::MakeWire({pathCoedge}, sggk::WireType::Open);

    const auto pathStart = pathCurve->CalcStart();
    sggk::Dir3D profileNormal(ctrlPoints[1][0] - ctrlPoints[0][0]);
    try
    {
        profileNormal = sggk::Dir3D(pathCurve->CalcDeriv1(pathCurve->Domain().Min()));
    }
    catch (...)
    {
    }
    sggk::Circle3D profileCircle(sggk::Ucs3D(pathStart, profileNormal), spec.profileRadius);
    auto profileEdge = sggk::TopoBuilder::MakeEdge(profileCircle);
    auto profileCoedge = sggk::TopoBuilder::MakeCoedge(profileEdge, true);
    auto profileWire = sggk::TopoBuilder::MakeWire({profileCoedge}, sggk::WireType::Closed);
    sggk::SurfacePtr profileSurface = std::make_shared<sggk::Plane>(pathStart, profileNormal);
    auto profileFace = sggk::api_create_face(profileWire, profileSurface, spec.operationTol, true);
    if (!profileFace)
    {
        throw std::runtime_error(role + " support_sweep_bspline_surface failed to create profile face");
    }

    sggk::SweepOpts opts(true);
    opts.SetSweepMode(sggk::SweepMode::SupportFace);
    opts.SetSupportBody(supportBody);
    opts.SetSolid(true);
    opts.SetG1Tol(spec.g1Tol);
    opts.SetModelingTol(spec.operationTol);
    opts.SetRelocateProfile(true);

    auto ret = sggk::api_sweep_entity(profileFace, pathWire, opts);
    auto body = FirstResultBody(ret, role + " support_sweep_bspline_surface");
    ApplyBodyTransform(spec, body);
    return body;
}

sggk::BodyPtr MakeRevolveLineBody(const BodySpec& spec, const std::string& role)
{
    RequirePositive(spec.bottomRadius, role + "_bottom_radius");
    RequirePositive(spec.topRadius, role + "_top_radius");
    RequirePositive(spec.height, role + "_height");
    RequirePositive(spec.angle, role + "_angle");
    RequirePositive(spec.operationTol, role + "_operation_tol");

    auto startVertex = sggk::TopoBuilder::MakeVertex(sggk::Point3D(spec.bottomRadius, 0.0, -0.5 * spec.height));
    auto endVertex = sggk::TopoBuilder::MakeVertex(sggk::Point3D(spec.topRadius, 0.0, 0.5 * spec.height));
    auto profileEdge = sggk::TopoBuilder::MakeLinearEdge(startVertex, endVertex);
    sggk::Axis1 axis(sggk::Point3D(0.0, 0.0, 0.0), sggk::Dir3D::UnitZ);

    sggk::RevolveOpts opts;
    opts.SetModelingTol(spec.operationTol);
    opts.SetCheckValid(true);
    opts.SetToTopoTrack(false);

    auto ret = sggk::api_revolve_entity(profileEdge, axis, spec.angle, opts);
    auto body = FirstResultBody(ret, role + " revolve_line");
    ApplyBodyTransform(spec, body);
    return body;
}

sggk::WirePtr MakeRadialRectProfileWire(double innerRadius, double outerRadius, double height)
{
    auto v0 = sggk::TopoBuilder::MakeVertex(sggk::Point3D(innerRadius, 0.0, -0.5 * height));
    auto v1 = sggk::TopoBuilder::MakeVertex(sggk::Point3D(outerRadius, 0.0, -0.5 * height));
    auto v2 = sggk::TopoBuilder::MakeVertex(sggk::Point3D(outerRadius, 0.0, 0.5 * height));
    auto v3 = sggk::TopoBuilder::MakeVertex(sggk::Point3D(innerRadius, 0.0, 0.5 * height));

    auto e0 = sggk::TopoBuilder::MakeLinearEdge(v0, v1);
    auto e1 = sggk::TopoBuilder::MakeLinearEdge(v1, v2);
    auto e2 = sggk::TopoBuilder::MakeLinearEdge(v2, v3);
    auto e3 = sggk::TopoBuilder::MakeLinearEdge(v3, v0);
    return sggk::TopoBuilder::MakeWire(
        {
            sggk::TopoBuilder::MakeCoedge(e0, true),
            sggk::TopoBuilder::MakeCoedge(e1, true),
            sggk::TopoBuilder::MakeCoedge(e2, true),
            sggk::TopoBuilder::MakeCoedge(e3, true),
        },
        sggk::WireType::Closed);
}

sggk::BodyPtr MakeRevolveRectBody(const BodySpec& spec, const std::string& role)
{
    RequirePositive(spec.innerRadius, role + "_inner_radius");
    RequirePositive(spec.outerRadius, role + "_outer_radius");
    RequirePositive(spec.height, role + "_height");
    RequirePositive(spec.angle, role + "_angle");
    RequirePositive(spec.operationTol, role + "_operation_tol");
    if (spec.outerRadius <= spec.innerRadius)
    {
        throw std::runtime_error(role + "_outer_radius must be greater than " + role + "_inner_radius");
    }

    auto profileWire = MakeRadialRectProfileWire(spec.innerRadius, spec.outerRadius, spec.height);
    sggk::SurfacePtr profileSurface = std::make_shared<sggk::Plane>(sggk::Point3D(0.0, 0.0, 0.0), sggk::Dir3D(0.0, 1.0, 0.0));
    auto profileFace = sggk::api_create_face(profileWire, profileSurface, spec.operationTol, true);
    if (!profileFace)
    {
        throw std::runtime_error(role + " revolve_rect failed to create profile face");
    }

    sggk::Axis1 axis(sggk::Point3D(0.0, 0.0, 0.0), sggk::Dir3D::UnitZ);
    sggk::RevolveOpts opts;
    opts.SetModelingTol(spec.operationTol);
    opts.SetCheckValid(true);
    opts.SetToTopoTrack(false);

    auto ret = sggk::api_revolve_entity(profileFace, axis, spec.angle, opts);
    auto body = FirstResultBody(ret, role + " revolve_rect");
    ApplyBodyTransform(spec, body);
    return body;
}

sggk::BodyPtr MakePreBooleanCylinderWedgeBody(const BodySpec& spec, const std::string& role)
{
    BodySpec base;
    base.kind = "solid_cylinder";
    base.radius = spec.radius;
    base.height = spec.height;
    base.angle = spec.angle;
    base.createSeamEdge = spec.createSeamEdge;

    BodySpec cutter;
    cutter.kind = "solid_wedge";
    cutter.length = spec.length;
    cutter.width = spec.width;
    cutter.height = spec.secondaryHeight;
    cutter.translateX = spec.secondaryTranslateX;
    cutter.translateY = spec.secondaryTranslateY;
    cutter.translateZ = spec.secondaryTranslateZ;

    auto target = MakePrimitiveBody(base, role + "_pre_target");
    auto tool = MakePrimitiveBody(cutter, role + "_pre_tool");

    sggk::BooleanOpts opts(ParseBooleanType(spec.booleanType));
    opts.SetModelingTol(spec.operationTol);
    opts.SetCheckValid(true);
    opts.SetToTopoTrack(false);
    opts.SetNonDestructive(true);

    auto ret = sggk::api_boolean(target, tool, opts);
    auto body = FirstResultBody(ret, role + " pre_boolean_cylinder_wedge");
    ApplyBodyTransform(spec, body);
    return body;
}

sggk::BodyPtr MakeBodyFromSpec(const BodySpec& spec, const std::string& role)
{
    if (IsPrimitiveKind(spec.kind))
    {
        return MakePrimitiveBody(spec, role);
    }
    if (spec.kind == "loaded_sgt")
    {
        return MakeLoadedSgtBody(spec, role);
    }
    if (spec.kind == "plane_sheet")
    {
        return MakePlaneSheetBody(spec, role);
    }
    if (spec.kind == "extrude_rect")
    {
        return MakeExtrudedRectBody(spec, role);
    }
    if (spec.kind == "thicken_rect_sheet")
    {
        return MakeThickenedRectSheetBody(spec, role);
    }
    if (spec.kind == "sweep_circle_line")
    {
        return MakeSweepCircleLineBody(spec, role);
    }
    if (spec.kind == "support_sweep_bspline_surface")
    {
        return MakeSupportSweepBSplineSurfaceBody(spec, role);
    }
    if (spec.kind == "revolve_line")
    {
        return MakeRevolveLineBody(spec, role);
    }
    if (spec.kind == "revolve_rect")
    {
        return MakeRevolveRectBody(spec, role);
    }
    if (spec.kind == "pre_boolean_cylinder_wedge")
    {
        return MakePreBooleanCylinderWedgeBody(spec, role);
    }
    throw std::runtime_error("unsupported " + role + "_kind: " + spec.kind);
}

void SerializeTopology(const sggk::TopologyPtr& topo, const fs::path& path)
{
    if (!topo)
    {
        return;
    }
    fs::create_directories(path.parent_path());
    sggk::RapidTopoJsonSerializer serializer;
    serializer.Serialize(topo, path.string().c_str());
}

std::string SanitizeFileStem(const std::string& value)
{
    std::string result;
    result.reserve(value.size());
    for (const char ch : value)
    {
        const unsigned char uch = static_cast<unsigned char>(ch);
        if (std::isalnum(uch) || ch == '_' || ch == '-' || ch == '.')
        {
            result.push_back(ch);
        }
        else
        {
            result.push_back('_');
        }
    }
    while (!result.empty() && (result.front() == '_' || result.front() == '.'))
    {
        result.erase(result.begin());
    }
    while (!result.empty() && (result.back() == '_' || result.back() == '.'))
    {
        result.pop_back();
    }
    return result.empty() ? std::string("asset") : result;
}

std::string BodySummaryJson(const sggk::BodyPtr& body)
{
    if (!body)
    {
        return "{}";
    }

    std::ostringstream os;
    os << "{"
       << "\"id\":" << body->ID()
       << ",\"shells\":" << body->QueryShells().size()
       << ",\"faces\":" << body->QueryFaces().size()
       << ",\"wires\":" << body->QueryWires().size()
       << ",\"edges\":" << body->QueryEdges().size()
       << ",\"vertices\":" << body->QueryVertices().size()
       << "}";
    return os.str();
}

std::string Offset2DStatusName(sggk::Offset2DStatus status)
{
    switch (status)
    {
    case sggk::Offset2DStatus::Success: return "Success";
    case sggk::Offset2DStatus::EmptyPath: return "EmptyPath";
    case sggk::Offset2DStatus::CanNotConnect: return "CanNotConnect";
    case sggk::Offset2DStatus::CrvReversed: return "CrvReversed";
    case sggk::Offset2DStatus::CrvDegenToPoint: return "CrvDegenToPoint";
    case sggk::Offset2DStatus::UnexpectedFailure: return "UnexpectedFailure";
    default: return "Unknown";
    }
}

sggk::Offset2DConnType ParseOffset2DConnType(const std::string& value)
{
    if (value == "DoNotConnect")
    {
        return sggk::Offset2DConnType::DoNotConnect;
    }
    if (value == "ByLineSeg")
    {
        return sggk::Offset2DConnType::ByLineSeg;
    }
    if (value == "ByArc")
    {
        return sggk::Offset2DConnType::ByArc;
    }
    throw std::runtime_error("unsupported offset2d_connect_type: " + value);
}

sggk::Offset2DExtendType ParseOffset2DExtendType(const std::string& value)
{
    if (value == "TangentExtend")
    {
        return sggk::Offset2DExtendType::TangentExtend;
    }
    if (value == "NatruralExtend" || value == "NaturalExtend")
    {
        return sggk::Offset2DExtendType::NatruralExtend;
    }
    throw std::runtime_error("unsupported offset2d_extend_type: " + value);
}

sggk::Point2D PointOnCircle(const Offset2DSegmentSpec& segment, double angle)
{
    return sggk::Point2D(
        segment.centerX + segment.radius * std::cos(angle),
        segment.centerY + segment.radius * std::sin(angle));
}

sggk::CoBoundedCrv2D MakeOffset2DSegment(const Offset2DSegmentSpec& segment)
{
    sggk::BoundedCurve2DPtr curve;
    if (segment.kind == "line")
    {
        curve = std::make_shared<sggk::TrimmedCurve2D>(
            sggk::Point2D(segment.x1, segment.y1),
            sggk::Point2D(segment.x2, segment.y2));
    }
    else if (segment.kind == "arc")
    {
        sggk::Ucs2D ucs(sggk::Point2D(segment.centerX, segment.centerY), sggk::Dir2D::UnitX);
        auto circle = std::make_shared<sggk::Circle2D>(ucs, segment.radius, segment.ccw);
        curve = std::make_shared<sggk::TrimmedCurve2D>(
            circle,
            PointOnCircle(segment, segment.startAngle),
            PointOnCircle(segment, segment.endAngle),
            sggk::Toler::Global());
    }
    else
    {
        throw std::runtime_error("unsupported offset2d segment kind: " + segment.kind);
    }
    return sggk::CoBoundedCrv2D(curve, segment.sense);
}

std::vector<sggk::CoBoundedCrv2D> MakeOffset2DPath(const Offset2DRecipe& recipe)
{
    std::vector<sggk::CoBoundedCrv2D> path;
    path.reserve(recipe.path.size());
    for (const auto& segment : recipe.path)
    {
        path.push_back(MakeOffset2DSegment(segment));
    }
    return path;
}

std::string Offset2DSegmentJson(const Offset2DSegmentSpec& segment)
{
    const sggk::Point2D start = segment.kind == "arc"
        ? PointOnCircle(segment, segment.startAngle)
        : sggk::Point2D(segment.x1, segment.y1);
    const sggk::Point2D end = segment.kind == "arc"
        ? PointOnCircle(segment, segment.endAngle)
        : sggk::Point2D(segment.x2, segment.y2);
    auto pointJson = [](const sggk::Point2D& point) {
        std::ostringstream pointOs;
        pointOs << "[" << std::setprecision(17) << point.X()
                << "," << std::setprecision(17) << point.Y() << "]";
        return pointOs.str();
    };
    std::ostringstream os;
    os << "{"
       << "\"kind\":\"" << EscapeJson(segment.kind) << "\""
       << ",\"sense\":" << (segment.sense ? "true" : "false")
       << ",\"start\":" << pointJson(start)
       << ",\"end\":" << pointJson(end)
       << ",\"center\":[" << std::setprecision(17) << segment.centerX << "," << segment.centerY << "]"
       << ",\"radius\":" << std::setprecision(17) << segment.radius
       << ",\"start_angle\":" << std::setprecision(17) << segment.startAngle
       << ",\"end_angle\":" << std::setprecision(17) << segment.endAngle
       << ",\"ccw\":" << (segment.ccw ? "true" : "false")
       << "}";
    return os.str();
}

std::string Offset2DRecipeJson(const Offset2DRecipe& recipe)
{
    std::ostringstream os;
    os << "{"
       << "\"distance\":" << std::setprecision(17) << recipe.distance
       << ",\"distances\":[";
    for (size_t i = 0; i < recipe.distances.size(); ++i)
    {
        if (i != 0)
        {
            os << ",";
        }
        os << std::setprecision(17) << recipe.distances[i];
    }
    os << "]"
       << ",\"dist_tol\":" << std::setprecision(17) << recipe.distTol
       << ",\"angle_tol\":" << std::setprecision(17) << recipe.angleTol
       << ",\"connect_type\":\"" << EscapeJson(recipe.connectType) << "\""
       << ",\"allow_crv_degenerated\":" << (recipe.allowCrvDegenerated ? "true" : "false")
       << ",\"allow_crv_reversed\":" << (recipe.allowCrvReversed ? "true" : "false")
       << ",\"allow_self_intersections\":" << (recipe.allowSelfIntersections ? "true" : "false")
       << ",\"extend_type\":\"" << EscapeJson(recipe.extendType) << "\""
       << ",\"expected_status\":\"" << EscapeJson(recipe.expectedStatus) << "\""
       << ",\"result_path_count_min_set\":" << (recipe.resultPathCountMinSet ? "true" : "false")
       << ",\"result_path_count_min\":" << recipe.resultPathCountMin
       << ",\"result_path_count_max_set\":" << (recipe.resultPathCountMaxSet ? "true" : "false")
       << ",\"result_path_count_max\":" << recipe.resultPathCountMax
       << ",\"path\":[";
    for (size_t i = 0; i < recipe.path.size(); ++i)
    {
        if (i != 0)
        {
            os << ",";
        }
        os << Offset2DSegmentJson(recipe.path[i]);
    }
    os << "]}";
    return os.str();
}

std::string CountExpectationJson(const CountExpectation& expectation)
{
    std::ostringstream os;
    os << "{"
       << "\"min_set\":" << (expectation.minSet ? "true" : "false")
       << ",\"min\":" << expectation.min
       << ",\"max_set\":" << (expectation.maxSet ? "true" : "false")
       << ",\"max\":" << expectation.max
       << "}";
    return os.str();
}

std::string SplitRecipeJson(const SplitRecipe& recipe)
{
    std::ostringstream os;
    os << "{"
       << "\"target_add_face\":" << (recipe.targetAddFace ? "true" : "false")
       << ",\"strict_split\":" << (recipe.strictSplit ? "true" : "false")
       << ",\"merge_imprint\":" << (recipe.mergeImprint ? "true" : "false")
       << ",\"outer_bodies\":" << CountExpectationJson(recipe.outerBodies)
       << ",\"inner_bodies\":" << CountExpectationJson(recipe.innerBodies)
       << ",\"wire_bodies\":" << CountExpectationJson(recipe.wireBodies)
       << ",\"total_bodies\":" << CountExpectationJson(recipe.totalBodies)
       << "}";
    return os.str();
}

std::string SliceRecipeJson(const SliceRecipe& recipe)
{
    std::ostringstream os;
    os << "{"
       << "\"result_bodies\":" << CountExpectationJson(recipe.resultBodies)
       << ",\"wire_bodies\":" << CountExpectationJson(recipe.wireBodies)
       << "}";
    return os.str();
}

std::string DslProvenanceJson(const CaseRecipe& recipe, int indent = 4)
{
    const std::string pad(indent, ' ');
    std::ostringstream os;
    os << "{\n"
       << pad << "\"source\": \"" << EscapeJson(recipe.dslSource) << "\",\n"
       << pad << "\"case_id\": \"" << EscapeJson(recipe.dslCaseId) << "\",\n"
       << pad << "\"variant\": \"" << EscapeJson(recipe.dslVariant) << "\",\n"
       << pad << "\"hypothesis\": \"" << EscapeJson(recipe.hypothesis) << "\",\n"
       << pad << "\"source_ref\": \"" << EscapeJson(recipe.sourceRef) << "\",\n"
       << pad << "\"source_task_id\": \"" << EscapeJson(recipe.sourceTaskId) << "\",\n"
       << pad << "\"source_task_path\": \"" << EscapeJson(recipe.sourceTaskPath) << "\",\n"
       << pad << "\"source_risk_id\": \"" << EscapeJson(recipe.sourceRiskId) << "\",\n"
       << pad << "\"source_risk_family\": \"" << EscapeJson(recipe.sourceRiskFamily) << "\",\n"
       << pad << "\"source_risk_categories\": \"" << EscapeJson(recipe.sourceRiskCategories) << "\"\n"
       << std::string(std::max(0, indent - 2), ' ') << "}";
    return os.str();
}

void WriteManifest(const CaseRecipe& recipe, const CliOptions& cli, const fs::path& caseDir)
{
    std::ostringstream os;
    os << "{\n"
       << "  \"case_id\": \"" << EscapeJson(recipe.caseId) << "\",\n"
       << "  \"api\": \"" << EscapeJson(recipe.api) << "\",\n"
       << "  \"created_at\": \"" << NowIsoLike() << "\",\n"
       << "  \"sggk_version\": \"" << SGGK_VERSION_STRING_EXT << "\",\n"
       << "  \"recipe_path\": \"" << EscapeJson(cli.recipePath.string()) << "\",\n"
       << "  \"dsl\": " << DslProvenanceJson(recipe, 4) << ",\n"
       << "  \"options\": {\n"
       << "    \"boolean_type\": \"" << EscapeJson(recipe.booleanType) << "\",\n"
       << "    \"modeling_tol\": " << std::setprecision(17) << recipe.modelingTol << ",\n"
       << "    \"offset_distance\": " << std::setprecision(17) << recipe.offsetDistance << ",\n"
       << "    \"max_model_size\": " << std::setprecision(17) << recipe.maxModelSize << ",\n"
       << "    \"check_valid\": " << (recipe.checkValid ? "true" : "false") << ",\n"
       << "    \"topo_track\": " << (recipe.topoTrack ? "true" : "false") << ",\n"
       << "    \"non_destructive\": " << (recipe.nonDestructive ? "true" : "false") << ",\n"
       << "    \"source_body_index\": " << recipe.sourceBodyIndex << ",\n"
       << "    \"step_app_protocol\": \"" << EscapeJson(recipe.stepAppProtocol) << "\",\n"
       << "    \"step_surface_to_bspline\": " << (recipe.stepSurfaceToBSpline ? "true" : "false") << ",\n"
       << "    \"step_curve_to_bspline\": " << (recipe.stepCurveToBSpline ? "true" : "false") << ",\n"
       << "    \"step_spcurve_in_wire_to_bspline\": " << (recipe.stepSpcurveInWireToBSpline ? "true" : "false") << ",\n"
       << "    \"iges_face_only_mode\": " << (recipe.igesFaceOnlyMode ? "true" : "false") << ",\n"
       << "    \"iges_write_sgk_specified_data\": " << (recipe.igesWriteSGKSpecifiedData ? "true" : "false") << ",\n"
       << "    \"roundtrip_abs_tol\": " << std::setprecision(17) << recipe.roundtripAbsTol << ",\n"
       << "    \"roundtrip_rel_tol\": " << std::setprecision(17) << recipe.roundtripRelTol << "\n"
       << "  },\n"
       << "  \"expectations\": " << ValidationExpectationsJson(recipe.expectations) << ",\n"
       << "  \"split\": " << SplitRecipeJson(recipe.split) << ",\n"
       << "  \"slice\": " << SliceRecipeJson(recipe.slice) << ",\n"
       << "  \"offset2d\": " << Offset2DRecipeJson(recipe.offset2d) << ",\n"
       << "  \"inputs\": {\n"
       << "    \"target\": " << BodySpecJson(recipe.boolean.target) << ",\n"
       << "    \"tool\": " << BodySpecJson(recipe.boolean.tool) << ",\n"
       << "    \"source\": " << BodySpecJson(recipe.offsetSource);
    if (!recipe.sourceFile.empty())
    {
        os << ",\n    \"source_file\": \"" << EscapeJson(recipe.sourceFile.string()) << "\",\n"
           << "    \"source_body_index\": " << recipe.sourceBodyIndex << "\n";
    }
    else
    {
        os << "\n";
    }
    os
       << "  }\n"
       << "}\n";
    WriteTextFile(caseDir / "manifest.json", os.str());
}

void WriteInputProvenance(
    const CaseRecipe& recipe,
    const sggk::BodyPtr& target,
    const sggk::BodyPtr& tool,
    const fs::path& caseDir)
{
    std::ostringstream os;
    os << "{\n"
       << "  \"dsl\": " << DslProvenanceJson(recipe, 4) << ",\n"
       << "  \"inputs\": [\n"
       << "    {\"role\":\"target\",\"body_id\":" << (target ? target->ID() : 0)
       << ",\"operations\":" << StringArrayJson(recipe.boolean.target.operations)
       << ",\"summary\":" << BodySummaryJson(target) << "},\n"
       << "    {\"role\":\"tool\",\"body_id\":" << (tool ? tool->ID() : 0)
       << ",\"operations\":" << StringArrayJson(recipe.boolean.tool.operations)
       << ",\"summary\":" << BodySummaryJson(tool) << "}\n"
       << "  ]\n"
       << "}\n";
    WriteTextFile(caseDir / "report" / "input_provenance.json", os.str());
}

void WriteInputTopologyIndex(
    const CaseRecipe& recipe,
    const InputTopologyIndex& index,
    const fs::path& caseDir)
{
    std::ostringstream os;
    os << "{\n"
       << "  \"dsl\": " << DslProvenanceJson(recipe, 4) << ",\n"
       << "  \"inputs\": [\n";

    const std::vector<std::string> roles = {"target", "tool"};
    for (size_t roleIndex = 0; roleIndex < roles.size(); ++roleIndex)
    {
        const auto& role = roles[roleIndex];
        if (roleIndex != 0)
        {
            os << ",\n";
        }

        std::vector<const TopologyRef*> refs;
        for (const auto& ref : index.entries)
        {
            if (ref.role == role)
            {
                refs.push_back(&ref);
            }
        }

        const auto& operations = role == "target"
            ? recipe.boolean.target.operations
            : recipe.boolean.tool.operations;

        sggk::ID bodyId = 0;
        for (const auto* ref : refs)
        {
            if (ref && ref->type == "Body")
            {
                bodyId = ref->bodyId;
                break;
            }
        }

        os << "    {\n"
           << "      \"role\": \"" << role << "\",\n"
           << "      \"body_id\": " << bodyId << ",\n"
           << "      \"operation_chain\": " << StringArrayJson(operations) << ",\n"
           << "      \"terminal_operation\": \"" << EscapeJson(LastStringOrEmpty(operations)) << "\",\n"
           << "      \"topology_count\": " << refs.size() << ",\n"
           << "      \"topologies\": [\n";

        for (size_t i = 0; i < refs.size(); ++i)
        {
            const auto& ref = *refs[i];
            if (i != 0)
            {
                os << ",\n";
            }
            os << "        {"
               << "\"type\":\"" << EscapeJson(ref.type) << "\""
               << ",\"id\":" << ref.id
               << ",\"local_index\":" << ref.localIndex
               << ",\"locator\":" << TopologyLocatorJson(ref.topology)
               << "}";
        }
        os << "\n"
           << "      ]\n"
           << "    }";
    }

    os << "\n"
       << "  ]\n"
       << "}\n";
    WriteTextFile(caseDir / "report" / "input_topology_index.json", os.str());
}

void CaptureErrorEntities(const sggk::ErrorInfo& status, const fs::path& caseDir)
{
    int index = 0;
    for (const auto& entity : status.ErrorEntities())
    {
        const auto topo = sggk::Entity::Cast<sggk::Topology>(entity);
        if (topo)
        {
            SerializeTopology(topo, caseDir / "output" / ("error_entity_" + std::to_string(++index) + ".sgt"));
        }
    }
}

void WriteStatusGeneric(
    bool succeeded,
    unsigned int errorCode,
    const std::string& errorMessage,
    size_t errorEntityCount,
    size_t resultBodyCount,
    size_t resultTopologyCount,
    const fs::path& caseDir)
{
    std::ostringstream os;
    os << "{\n"
       << "  \"succeeded\": " << (succeeded ? "true" : "false") << ",\n"
       << "  \"error_code\": " << errorCode << ",\n"
       << "  \"error_message\": \"" << EscapeJson(errorMessage) << "\",\n"
       << "  \"error_entity_count\": " << errorEntityCount << ",\n"
       << "  \"result_body_count\": " << resultBodyCount << ",\n"
       << "  \"result_topology_count\": " << resultTopologyCount << "\n"
       << "}\n";
    WriteTextFile(caseDir / "report" / "status.json", os.str());
}

void WriteStatusGeneric(
    bool succeeded,
    unsigned int errorCode,
    const std::string& errorMessage,
    size_t errorEntityCount,
    size_t resultBodyCount,
    const fs::path& caseDir)
{
    WriteStatusGeneric(succeeded, errorCode, errorMessage, errorEntityCount, resultBodyCount, resultBodyCount, caseDir);
}

void WriteStatus(const sggk::ModelingRetPtr& ret, const fs::path& caseDir)
{
    const auto& status = ret->Status();
    WriteStatusGeneric(
        ret->Succeeded(),
        status.ErrorCode(),
        status.ErrorMsg(),
        status.ErrorEntities().size(),
        ret->ResultBodies().size(),
        caseDir);
}

std::string Point2DValueJson(const sggk::Point2D& point)
{
    std::ostringstream os;
    os << "[" << std::setprecision(17) << point.X()
       << "," << std::setprecision(17) << point.Y() << "]";
    return os.str();
}

std::string Offset2DPathSegmentJson(const sggk::CoBoundedCrv2D& segment, size_t index)
{
    std::ostringstream os;
    os << "{"
       << "\"index\":" << index
       << ",\"sense\":" << (segment.Sense() ? "true" : "false");
    try
    {
        os << ",\"start\":" << Point2DValueJson(segment.CalcStart())
           << ",\"end\":" << Point2DValueJson(segment.CalcEnd());
    }
    catch (const std::exception& ex)
    {
        os << ",\"point_error\":\"" << EscapeJson(ex.what()) << "\"";
    }
    catch (...)
    {
        os << ",\"point_error\":\"unknown\"";
    }
    if (segment.BoundedCurve())
    {
        os << ",\"curve_type\":" << static_cast<int>(segment.BoundedCurve()->CurveType());
    }
    os << "}";
    return os.str();
}

std::string Offset2DIndexMapJson(const std::vector<std::vector<sggk::Integer>>& indexMap)
{
    std::ostringstream os;
    os << "[";
    for (size_t i = 0; i < indexMap.size(); ++i)
    {
        if (i != 0)
        {
            os << ",";
        }
        os << "[";
        for (size_t j = 0; j < indexMap[i].size(); ++j)
        {
            if (j != 0)
            {
                os << ",";
            }
            os << indexMap[i][j];
        }
        os << "]";
    }
    os << "]";
    return os.str();
}

void WriteOffset2DResult(const sggk::Offset2DResult& result, const fs::path& caseDir)
{
    std::ostringstream os;
    os << "{\n"
       << "  \"status\": \"" << Offset2DStatusName(result.status) << "\",\n"
       << "  \"status_code\": " << static_cast<unsigned int>(result.status) << ",\n"
       << "  \"result_path_count\": " << result.resultPaths.size() << ",\n"
       << "  \"paths\": [";
    for (size_t i = 0; i < result.resultPaths.size(); ++i)
    {
        if (i != 0)
        {
            os << ",";
        }
        os << "\n    {\"index\":" << i
           << ",\"segment_count\":" << result.resultPaths[i].size()
           << ",\"segments\":[";
        for (size_t j = 0; j < result.resultPaths[i].size(); ++j)
        {
            if (j != 0)
            {
                os << ",";
            }
            os << Offset2DPathSegmentJson(result.resultPaths[i][j], j);
        }
        os << "]}";
    }
    os << "\n  ],\n"
       << "  \"index_map\": " << Offset2DIndexMapJson(result.indexMap) << "\n"
       << "}\n";
    WriteTextFile(caseDir / "report" / "offset2d_result.json", os.str());
}

void WriteOffset2DStatus(
    const CaseRecipe& recipe,
    const sggk::Offset2DResult& result,
    const fs::path& caseDir)
{
    const std::string actualStatus = Offset2DStatusName(result.status);
    const bool apiSucceeded = result.status == sggk::Offset2DStatus::Success;
    const bool expectedStatusMatched = actualStatus == recipe.offset2d.expectedStatus;
    std::ostringstream os;
    os << "{\n"
       << "  \"succeeded\": " << (apiSucceeded ? "true" : "false") << ",\n"
       << "  \"error_code\": " << static_cast<unsigned int>(result.status) << ",\n"
       << "  \"error_message\": \"" << EscapeJson(actualStatus) << "\",\n"
       << "  \"error_entity_count\": 0,\n"
       << "  \"result_body_count\": 0,\n"
       << "  \"result_topology_count\": " << result.resultPaths.size() << ",\n"
       << "  \"status_semantics\": \"offset2d_status_enum\",\n"
       << "  \"expected_status\": \"" << EscapeJson(recipe.offset2d.expectedStatus) << "\",\n"
       << "  \"actual_status\": \"" << EscapeJson(actualStatus) << "\",\n"
       << "  \"expected_status_matched\": " << (expectedStatusMatched ? "true" : "false") << ",\n"
       << "  \"test_outcome_succeeded\": " << (expectedStatusMatched ? "true" : "false") << "\n"
       << "}\n";
    WriteTextFile(caseDir / "report" / "status.json", os.str());
}

bool WriteOffset2DValidation(
    const CaseRecipe& recipe,
    const sggk::Offset2DResult& result,
    const fs::path& caseDir)
{
    std::vector<std::string> failures;
    const auto& expected = recipe.offset2d;
    const std::string actualStatus = Offset2DStatusName(result.status);
    if (actualStatus != expected.expectedStatus)
    {
        failures.push_back("offset2d_status_mismatch actual=" + actualStatus + " expected=" + expected.expectedStatus);
    }
    const int pathCount = static_cast<int>(result.resultPaths.size());
    if (expected.resultPathCountMinSet && pathCount < expected.resultPathCountMin)
    {
        failures.push_back("offset2d_result_path_count_below_min actual=" + std::to_string(pathCount) +
            " min=" + std::to_string(expected.resultPathCountMin));
    }
    if (expected.resultPathCountMaxSet && pathCount > expected.resultPathCountMax)
    {
        failures.push_back("offset2d_result_path_count_above_max actual=" + std::to_string(pathCount) +
            " max=" + std::to_string(expected.resultPathCountMax));
    }

    std::ostringstream os;
    os << "{\n"
       << "  \"ok\": " << (failures.empty() ? "true" : "false") << ",\n"
       << "  \"status_semantics\": \"offset2d_status_enum\",\n"
       << "  \"expected_status\": \"" << EscapeJson(expected.expectedStatus) << "\",\n"
       << "  \"actual_status\": \"" << EscapeJson(actualStatus) << "\",\n"
       << "  \"expected_status_matched\": " << (actualStatus == expected.expectedStatus ? "true" : "false") << ",\n"
       << "  \"test_outcome_succeeded\": " << (failures.empty() ? "true" : "false") << ",\n"
       << "  \"offset2d_expected_status\": \"" << EscapeJson(expected.expectedStatus) << "\",\n"
       << "  \"offset2d_actual_status\": \"" << EscapeJson(actualStatus) << "\",\n"
       << "  \"offset2d_result_path_count\": " << pathCount << ",\n"
       << "  \"offset2d_expectations\": " << Offset2DRecipeJson(expected) << ",\n"
       << "  \"failures\": " << StringArrayJson(failures) << "\n"
       << "}\n";
    WriteTextFile(caseDir / "report" / "validation.json", os.str());
    return failures.empty();
}

template <typename BodyRange>
void AppendBodies(std::vector<sggk::BodyPtr>& output, const BodyRange& bodies)
{
    for (const auto& body : bodies)
    {
        if (body)
        {
            output.push_back(body);
        }
    }
}

bool AddCountExpectationFailures(
    const std::string& label,
    int actual,
    const CountExpectation& expectation,
    std::vector<std::string>& failures)
{
    bool ok = true;
    if (expectation.minSet && actual < expectation.min)
    {
        failures.push_back(label + "_below_min actual=" + std::to_string(actual) +
            " min=" + std::to_string(expectation.min));
        ok = false;
    }
    if (expectation.maxSet && actual > expectation.max)
    {
        failures.push_back(label + "_above_max actual=" + std::to_string(actual) +
            " max=" + std::to_string(expectation.max));
        ok = false;
    }
    return ok;
}

std::string CountCheckJson(
    const std::string& label,
    int actual,
    const CountExpectation& expectation,
    std::vector<std::string>& failures)
{
    const bool ok = AddCountExpectationFailures(label, actual, expectation, failures);
    std::ostringstream os;
    os << "{"
       << "\"actual\":" << actual
       << ",\"expectation\":" << CountExpectationJson(expectation)
       << ",\"ok\":" << (ok ? "true" : "false")
       << "}";
    return os.str();
}

std::string SplitResultJson(
    const SplitRecipe& recipe,
    const sggk::BodyList& outerBodies,
    const sggk::BodyList& innerBodies,
    const sggk::BodyList& wireBodies,
    std::vector<std::string>& failures)
{
    const int outerCount = static_cast<int>(outerBodies.size());
    const int innerCount = static_cast<int>(innerBodies.size());
    const int wireCount = static_cast<int>(wireBodies.size());
    const int totalCount = outerCount + innerCount + wireCount;
    std::ostringstream os;
    os << "{"
       << "\"outer_body_count\":" << outerCount
       << ",\"inner_body_count\":" << innerCount
       << ",\"wire_body_count\":" << wireCount
       << ",\"total_body_count\":" << totalCount
       << ",\"checks\":{"
       << "\"outer_bodies\":" << CountCheckJson("split_outer_bodies", outerCount, recipe.outerBodies, failures)
       << ",\"inner_bodies\":" << CountCheckJson("split_inner_bodies", innerCount, recipe.innerBodies, failures)
       << ",\"wire_bodies\":" << CountCheckJson("split_wire_bodies", wireCount, recipe.wireBodies, failures)
       << ",\"total_bodies\":" << CountCheckJson("split_total_bodies", totalCount, recipe.totalBodies, failures)
       << "}}";
    return os.str();
}

std::string SliceResultJson(
    const SliceRecipe& recipe,
    const std::vector<sggk::BodyPtr>& resultBodies,
    std::vector<std::string>& failures)
{
    const int resultCount = static_cast<int>(resultBodies.size());
    const int wireCount = resultCount;
    std::ostringstream os;
    os << "{"
       << "\"result_body_count\":" << resultCount
       << ",\"wire_body_count\":" << wireCount
       << ",\"checks\":{"
       << "\"result_bodies\":" << CountCheckJson("slice_result_bodies", resultCount, recipe.resultBodies, failures)
       << ",\"wire_bodies\":" << CountCheckJson("slice_wire_bodies", wireCount, recipe.wireBodies, failures)
       << "}}";
    return os.str();
}

std::string TopologySectionResultJson(
    const TopologySectionRecipe& recipe,
    int edgeCount,
    int vertexCount,
    std::vector<std::string>& failures)
{
    const int totalCount = edgeCount + vertexCount;
    std::ostringstream os;
    os << "{"
       << "\"edge_count\":" << edgeCount
       << ",\"vertex_count\":" << vertexCount
       << ",\"total_count\":" << totalCount
       << ",\"checks\":{"
       << "\"edges\":" << CountCheckJson("topology_section_edges", edgeCount, recipe.edges, failures)
       << ",\"vertices\":" << CountCheckJson("topology_section_vertices", vertexCount, recipe.vertices, failures)
       << ",\"total\":" << CountCheckJson("topology_section_total", totalCount, recipe.total, failures)
       << "}}";
    return os.str();
}

bool WriteTopoCheck(const std::vector<sggk::BodyPtr>& bodies, const fs::path& caseDir)
{
    std::ostringstream os;
    os << "{\n  \"bodies\": [\n";
    bool first = true;
    bool allOk = true;
    int index = 0;
    for (const auto& body : bodies)
    {
        if (!body)
        {
            continue;
        }
        sggk::TopoError error;
        const bool ok = sggk::TopoCheckTool::CheckBody(body, error);
        allOk = allOk && ok;
        if (!first)
        {
            os << ",\n";
        }
        first = false;
        os << "    {\"index\":" << index
           << ",\"body_id\":" << body->ID()
           << ",\"ok\":" << (ok ? "true" : "false");
        if (!ok)
        {
            os << ",\"error_code\":" << static_cast<unsigned int>(error.errorCode)
               << ",\"error_string\":\"" << EscapeJson(error.ErrorString()) << "\"";
            if (error.topo)
            {
                os << ",\"error_topology\":{\"id\":" << error.topo->ID()
                   << ",\"type\":\"" << TopoTypeName(error.topo->TopoType()) << "\"}";
                SerializeTopology(error.topo, caseDir / "output" / ("topo_check_error_" + std::to_string(index) + ".sgt"));
            }
        }
        os << "}";
        ++index;
    }
    os << "\n  ]\n}\n";
    WriteTextFile(caseDir / "report" / "topo_check.json", os.str());
    return allOk;
}

bool WriteTopoCheckTopologies(const std::vector<sggk::TopologyPtr>& topologies, const fs::path& caseDir)
{
    std::ostringstream os;
    os << "{\n  \"topologies\": [\n";
    bool first = true;
    bool allOk = true;
    int index = 0;
    for (const auto& topology : topologies)
    {
        if (!topology)
        {
            continue;
        }
        sggk::TopoError error;
        const bool ok = sggk::TopoCheckTool::CheckTopology(topology, error);
        allOk = allOk && ok;
        if (!first)
        {
            os << ",\n";
        }
        first = false;
        os << "    {\"index\":" << index
           << ",\"id\":" << topology->ID()
           << ",\"type\":\"" << TopoTypeName(topology->TopoType()) << "\""
           << ",\"ok\":" << (ok ? "true" : "false");
        if (!ok)
        {
            os << ",\"error_code\":" << static_cast<unsigned int>(error.errorCode)
               << ",\"error_string\":\"" << EscapeJson(error.ErrorString()) << "\"";
            if (error.topo)
            {
                os << ",\"error_topology\":{\"id\":" << error.topo->ID()
                   << ",\"type\":\"" << TopoTypeName(error.topo->TopoType()) << "\"}";
                SerializeTopology(error.topo, caseDir / "output" / ("topo_check_error_" + std::to_string(index) + ".sgt"));
            }
        }
        os << "}";
        ++index;
    }
    os << "\n  ]\n}\n";
    WriteTextFile(caseDir / "report" / "topo_check.json", os.str());
    return allOk;
}

double CalcBodyEdgeLength(const sggk::BodyPtr& body);

void CaptureBodyBBox(const sggk::BodyPtr& body, BodyProperties& item)
{
    if (!body)
    {
        item.bboxJson = "null";
        return;
    }

    try
    {
        const auto box = body->CalcBndBox(true);
        item.bboxJson = BndBoxObjectJson(box);
        if (!box.IsEmpty())
        {
            const auto minPoint = box.MinPoint();
            const auto maxPoint = box.MaxPoint();
            item.bboxOk = true;
            item.minX = minPoint.X();
            item.minY = minPoint.Y();
            item.minZ = minPoint.Z();
            item.maxX = maxPoint.X();
            item.maxY = maxPoint.Y();
            item.maxZ = maxPoint.Z();
        }
    }
    catch (const std::exception& ex)
    {
        item.bboxJson = "{\"error\":\"" + EscapeJson(ex.what()) + "\"}";
    }
    catch (...)
    {
        item.bboxJson = "{\"error\":\"unknown\"}";
    }
}

std::vector<BodyProperties> ComputeBodyProperties(const std::vector<sggk::BodyPtr>& bodies, bool measureProperties = true)
{
    std::vector<BodyProperties> properties;
    int index = 0;
    for (const auto& body : bodies)
    {
        if (!body)
        {
            ++index;
            continue;
        }

        BodyProperties item;
        item.index = index;
        item.bodyId = body->ID();
        item.summaryJson = BodySummaryJson(body);
        CaptureBodyBBox(body, item);
        if (measureProperties)
        {
            try
            {
                item.length = CalcBodyEdgeLength(body);
                item.area = sggk::TopoPropertyTool::CalcArea(body);
                item.volume = sggk::TopoPropertyTool::CalcVolume(body);
                item.propertyOk = true;
            }
            catch (const std::exception& ex)
            {
                item.propertyError = ex.what();
            }
            catch (...)
            {
                item.propertyError = "unknown exception";
            }
        }
        else
        {
            item.propertyError = "property sampling disabled";
        }
        properties.push_back(item);
        ++index;
    }
    return properties;
}

std::string BodyPropertyJson(const BodyProperties& property)
{
    std::ostringstream os;
    os << "{\"index\":" << property.index
       << ",\"body_id\":" << property.bodyId
       << ",\"summary\":" << property.summaryJson
       << ",\"bbox\":" << property.bboxJson
       << ",\"property_ok\":" << (property.propertyOk ? "true" : "false");
    if (property.propertyOk)
    {
        os << ",\"length\":" << std::setprecision(17) << property.length
           << ",\"area\":" << std::setprecision(17) << property.area
           << ",\"volume\":" << std::setprecision(17) << property.volume;
    }
    else
    {
        os << ",\"property_error\":\"" << EscapeJson(property.propertyError) << "\"";
    }
    os << "}";
    return os.str();
}

void WriteProperties(const std::vector<BodyProperties>& properties, const fs::path& caseDir)
{
    std::ostringstream os;
    os << "{\n  \"bodies\": [\n";
    bool first = true;
    for (const auto& property : properties)
    {
        if (!first)
        {
            os << ",\n";
        }
        first = false;
        os << "    " << BodyPropertyJson(property);
    }
    os << "\n  ]\n}\n";
    WriteTextFile(caseDir / "report" / "properties.json", os.str());
}

void WriteInputProperties(
    const std::vector<BodyProperties>& targetProperties,
    const std::vector<BodyProperties>& toolProperties,
    const fs::path& caseDir)
{
    auto writeArray = [](std::ostream& os, const std::vector<BodyProperties>& properties) {
        os << "[";
        for (size_t i = 0; i < properties.size(); ++i)
        {
            if (i != 0)
            {
                os << ",";
            }
            os << BodyPropertyJson(properties[i]);
        }
        os << "]";
    };

    std::ostringstream os;
    os << "{\n  \"target\": ";
    writeArray(os, targetProperties);
    os << ",\n  \"tool\": ";
    writeArray(os, toolProperties);
    os << "\n}\n";
    WriteTextFile(caseDir / "report" / "input_properties.json", os.str());
}

double CalcBodyEdgeLength(const sggk::BodyPtr& body)
{
    double total = 0.0;
    if (!body)
    {
        return total;
    }
    for (const auto& edge : body->QueryEdges())
    {
        if (edge)
        {
            total += edge->StartPoint().DistanceTo(edge->EndPoint());
        }
    }
    return total;
}

double TotalMetric(const std::vector<BodyProperties>& properties, double BodyProperties::*member)
{
    double total = 0.0;
    for (const auto& property : properties)
    {
        if (property.propertyOk)
        {
            total += property.*member;
        }
    }
    return total;
}

double TotalAbsVolume(const std::vector<BodyProperties>& properties)
{
    double total = 0.0;
    for (const auto& property : properties)
    {
        if (property.propertyOk)
        {
            total += std::fabs(property.volume);
        }
    }
    return total;
}

double CompareTolerance(double absTol, double relTol, double actual, double expected)
{
    return absTol + relTol * std::max(std::fabs(actual), std::fabs(expected));
}

bool LessOrEqualWithTol(double actual, double limit, double absTol, double relTol)
{
    return actual <= limit + CompareTolerance(absTol, relTol, actual, limit);
}

bool GreaterOrEqualWithTol(double actual, double limit, double absTol, double relTol)
{
    return actual + CompareTolerance(absTol, relTol, actual, limit) >= limit;
}

void AddMetricExpectationFailures(
    const std::string& name,
    double actual,
    const NumericExpectation& expectation,
    std::vector<std::string>& failures)
{
    if (expectation.minSet && !GreaterOrEqualWithTol(actual, expectation.minValue, expectation.absTol, expectation.relTol))
    {
        std::ostringstream os;
        os << name << "_below_min actual=" << std::setprecision(17) << actual
           << " min=" << std::setprecision(17) << expectation.minValue;
        failures.push_back(os.str());
    }
    if (expectation.maxSet && !LessOrEqualWithTol(actual, expectation.maxValue, expectation.absTol, expectation.relTol))
    {
        std::ostringstream os;
        os << name << "_above_max actual=" << std::setprecision(17) << actual
           << " max=" << std::setprecision(17) << expectation.maxValue;
        failures.push_back(os.str());
    }
    if (expectation.expectedSet)
    {
        const double tol = CompareTolerance(expectation.absTol, expectation.relTol, actual, expectation.expectedValue);
        if (std::fabs(actual - expectation.expectedValue) > tol)
        {
            std::ostringstream os;
            os << name << "_not_expected actual=" << std::setprecision(17) << actual
               << " expected=" << std::setprecision(17) << expectation.expectedValue
               << " tol=" << std::setprecision(17) << tol;
            failures.push_back(os.str());
        }
    }
}

void AddBooleanVolumeRelationFailures(
    const CaseRecipe& recipe,
    double targetAbsVolume,
    double toolAbsVolume,
    double resultAbsVolume,
    std::vector<std::string>& failures)
{
    const auto& expectations = recipe.expectations;
    const std::string type = recipe.booleanType;
    if (type == "SUBTRACTION")
    {
        if (!LessOrEqualWithTol(resultAbsVolume, targetAbsVolume, expectations.relationAbsTol, expectations.relationRelTol))
        {
            failures.push_back("boolean_subtraction_volume_exceeds_target");
        }
        return;
    }
    if (type == "INTERSECTION")
    {
        const double limit = std::min(targetAbsVolume, toolAbsVolume);
        if (!LessOrEqualWithTol(resultAbsVolume, limit, expectations.relationAbsTol, expectations.relationRelTol))
        {
            failures.push_back("boolean_intersection_volume_exceeds_input");
        }
        return;
    }
    if (type == "UNION")
    {
        if (!GreaterOrEqualWithTol(resultAbsVolume, std::max(targetAbsVolume, toolAbsVolume), expectations.relationAbsTol, expectations.relationRelTol))
        {
            failures.push_back("boolean_union_volume_below_input");
        }
        if (!LessOrEqualWithTol(resultAbsVolume, targetAbsVolume + toolAbsVolume, expectations.relationAbsTol, expectations.relationRelTol))
        {
            failures.push_back("boolean_union_volume_exceeds_sum");
        }
    }
}

struct BBoxAggregate
{
    bool ok = false;
    double minX = 0.0;
    double minY = 0.0;
    double minZ = 0.0;
    double maxX = 0.0;
    double maxY = 0.0;
    double maxZ = 0.0;
};

BBoxAggregate AggregateBBox(const std::vector<BodyProperties>& properties)
{
    BBoxAggregate aggregate;
    for (const auto& property : properties)
    {
        if (!property.bboxOk)
        {
            continue;
        }
        if (!aggregate.ok)
        {
            aggregate.ok = true;
            aggregate.minX = property.minX;
            aggregate.minY = property.minY;
            aggregate.minZ = property.minZ;
            aggregate.maxX = property.maxX;
            aggregate.maxY = property.maxY;
            aggregate.maxZ = property.maxZ;
            continue;
        }
        aggregate.minX = std::min(aggregate.minX, property.minX);
        aggregate.minY = std::min(aggregate.minY, property.minY);
        aggregate.minZ = std::min(aggregate.minZ, property.minZ);
        aggregate.maxX = std::max(aggregate.maxX, property.maxX);
        aggregate.maxY = std::max(aggregate.maxY, property.maxY);
        aggregate.maxZ = std::max(aggregate.maxZ, property.maxZ);
    }
    return aggregate;
}

BBoxAggregate UnionBBox(const BBoxAggregate& first, const BBoxAggregate& second)
{
    if (!first.ok)
    {
        return second;
    }
    if (!second.ok)
    {
        return first;
    }
    BBoxAggregate result;
    result.ok = true;
    result.minX = std::min(first.minX, second.minX);
    result.minY = std::min(first.minY, second.minY);
    result.minZ = std::min(first.minZ, second.minZ);
    result.maxX = std::max(first.maxX, second.maxX);
    result.maxY = std::max(first.maxY, second.maxY);
    result.maxZ = std::max(first.maxZ, second.maxZ);
    return result;
}

bool BBoxContainsWithTol(const BBoxAggregate& outer, const BBoxAggregate& inner, double tol)
{
    if (!outer.ok || !inner.ok)
    {
        return false;
    }
    return inner.minX >= outer.minX - tol &&
           inner.minY >= outer.minY - tol &&
           inner.minZ >= outer.minZ - tol &&
           inner.maxX <= outer.maxX + tol &&
           inner.maxY <= outer.maxY + tol &&
           inner.maxZ <= outer.maxZ + tol;
}

bool AllBBoxesOk(const std::vector<BodyProperties>& properties)
{
    if (properties.empty())
    {
        return false;
    }
    for (const auto& property : properties)
    {
        if (!property.bboxOk)
        {
            return false;
        }
    }
    return true;
}

bool AllPropertiesOk(const std::vector<BodyProperties>& properties)
{
    if (properties.empty())
    {
        return false;
    }
    for (const auto& property : properties)
    {
        if (!property.propertyOk)
        {
            return false;
        }
    }
    return true;
}

void AddBooleanBBoxRelationDiagnostics(
    const CaseRecipe& recipe,
    const std::vector<BodyProperties>& resultProperties,
    const std::vector<BodyProperties>& targetProperties,
    const std::vector<BodyProperties>& toolProperties,
    std::vector<std::string>& diagnostics)
{
    if (resultProperties.empty())
    {
        return;
    }
    if (!AllBBoxesOk(resultProperties))
    {
        diagnostics.push_back("boolean_bbox_relation_result_bbox_unavailable");
        return;
    }
    if (!AllBBoxesOk(targetProperties) || !AllBBoxesOk(toolProperties))
    {
        diagnostics.push_back("boolean_bbox_relation_input_bbox_unavailable");
        return;
    }

    const double tol = recipe.expectations.relationAbsTol;
    const auto resultBox = AggregateBBox(resultProperties);
    const auto targetBox = AggregateBBox(targetProperties);
    const auto toolBox = AggregateBBox(toolProperties);
    const std::string type = recipe.booleanType;

    if (type == "SUBTRACTION")
    {
        if (!BBoxContainsWithTol(targetBox, resultBox, tol))
        {
            diagnostics.push_back("boolean_subtraction_bbox_outside_target");
        }
        return;
    }
    if (type == "INTERSECTION")
    {
        if (!BBoxContainsWithTol(targetBox, resultBox, tol))
        {
            diagnostics.push_back("boolean_intersection_bbox_outside_target");
        }
        if (!BBoxContainsWithTol(toolBox, resultBox, tol))
        {
            diagnostics.push_back("boolean_intersection_bbox_outside_tool");
        }
        return;
    }
    if (type == "UNION")
    {
        const auto inputUnion = UnionBBox(targetBox, toolBox);
        if (!BBoxContainsWithTol(resultBox, targetBox, tol))
        {
            diagnostics.push_back("boolean_union_bbox_missing_target");
        }
        if (!BBoxContainsWithTol(resultBox, toolBox, tol))
        {
            diagnostics.push_back("boolean_union_bbox_missing_tool");
        }
        if (!BBoxContainsWithTol(inputUnion, resultBox, tol))
        {
            diagnostics.push_back("boolean_union_bbox_exceeds_input_union");
        }
    }
}

std::string BodyPtRelTypeName(sggk::BodyPtRelType relation)
{
    switch (relation)
    {
    case sggk::BodyPtRelType::Unknown: return "Unknown";
    case sggk::BodyPtRelType::OnVertex: return "OnVertex";
    case sggk::BodyPtRelType::OnEdge: return "OnEdge";
    case sggk::BodyPtRelType::OnFace: return "OnFace";
    case sggk::BodyPtRelType::Inside: return "Inside";
    case sggk::BodyPtRelType::Outside: return "Outside";
    }
    return "Unknown";
}

bool BodyPtRelationMatches(const std::string& expected, const std::string& actual)
{
    if (expected == actual)
    {
        return true;
    }
    if (expected == "OnBoundary")
    {
        return actual == "OnVertex" || actual == "OnEdge" || actual == "OnFace";
    }
    if (expected == "OnModel")
    {
        return actual == "OnVertex" || actual == "OnEdge" || actual == "OnFace" || actual == "Inside";
    }
    return false;
}

std::string FacePtRelTypeName(sggk::FacePtRelType relation)
{
    switch (relation)
    {
    case sggk::FacePtRelType::Unknown: return "Unknown";
    case sggk::FacePtRelType::OnVertex: return "OnVertex";
    case sggk::FacePtRelType::OnEdge: return "OnEdge";
    case sggk::FacePtRelType::Inside: return "Inside";
    case sggk::FacePtRelType::Outside: return "Outside";
    }
    return "Unknown";
}

bool FacePtRelationMatches(const std::string& expected, const std::string& actual)
{
    if (expected == actual)
    {
        return true;
    }
    if (expected == "OnBoundary")
    {
        return actual == "OnVertex" || actual == "OnEdge";
    }
    if (expected == "OnFace")
    {
        return actual == "OnVertex" || actual == "OnEdge" || actual == "Inside";
    }
    return false;
}

const std::vector<sggk::BodyPtr>* SelectRoleBodies(
    const std::string& role,
    const std::vector<sggk::BodyPtr>& resultBodies,
    const std::vector<sggk::BodyPtr>& targetBodies,
    const std::vector<sggk::BodyPtr>& toolBodies)
{
    if (role == "result")
    {
        return &resultBodies;
    }
    if (role == "target")
    {
        return &targetBodies;
    }
    if (role == "tool")
    {
        return &toolBodies;
    }
    return nullptr;
}

sggk::FacePtr SelectFace(
    const sggk::BodyPtr& body,
    int faceIndex,
    sggk::ID faceId,
    bool useFaceId)
{
    if (!body)
    {
        return nullptr;
    }
    const auto faces = body->QueryFaces();
    if (useFaceId)
    {
        for (const auto& face : faces)
        {
            if (face && face->ID() == faceId)
            {
                return face;
            }
        }
        return nullptr;
    }
    if (faceIndex < 0 || faceIndex >= static_cast<int>(faces.size()))
    {
        return nullptr;
    }
    int index = 0;
    for (const auto& face : faces)
    {
        if (index == faceIndex)
        {
            return face;
        }
        ++index;
    }
    return nullptr;
}

std::string Point2DJson(const sggk::Point2D& point)
{
    std::ostringstream os;
    os << "[" << std::setprecision(17) << point.X()
       << "," << std::setprecision(17) << point.Y() << "]";
    return os.str();
}

std::string UVRangeJson(const sggk::UVRange& range)
{
    std::ostringstream os;
    os << "{"
       << "\"u\":[" << std::setprecision(17) << range.IntervalU().Min()
       << "," << std::setprecision(17) << range.IntervalU().Max() << "]"
       << ",\"v\":[" << std::setprecision(17) << range.IntervalV().Min()
       << "," << std::setprecision(17) << range.IntervalV().Max() << "]"
       << "}";
    return os.str();
}

sggk::Point2D UVFromFraction(const sggk::UVRange& range, double uFraction, double vFraction)
{
    const double minU = range.IntervalU().Min();
    const double maxU = range.IntervalU().Max();
    const double minV = range.IntervalV().Min();
    const double maxV = range.IntervalV().Max();
    return sggk::Point2D(
        minU + (maxU - minU) * uFraction,
        minV + (maxV - minV) * vFraction);
}

std::string FacePtInfoTargetJson(const sggk::FacePtRelInfo& info)
{
    std::ostringstream os;
    os << "{";
    bool wrote = false;
    auto writeTarget = [&](const std::string& key, const sggk::TopologyPtr& topology) {
        if (!topology)
        {
            return;
        }
        if (wrote)
        {
            os << ",";
        }
        os << "\"" << key << "\":{\"type\":\"" << TopoTypeName(topology->TopoType())
           << "\",\"id\":" << topology->ID() << "}";
        wrote = true;
    };
    writeTarget("vertex", info.targetVertex);
    writeTarget("edge", info.targetEdge);
    os << "}";
    return os.str();
}

std::string ClashTypeName(sggk::ClashType type)
{
    switch (type)
    {
    case sggk::ClashType::Clash_None: return "Clash_None";
    case sggk::ClashType::Clash_Exists: return "Clash_Exists";
    case sggk::ClashType::Clash_AInB: return "Clash_AInB";
    case sggk::ClashType::Clash_BInA: return "Clash_BInA";
    case sggk::ClashType::Clash_Touch: return "Clash_Touch";
    case sggk::ClashType::Clash_Interfere: return "Clash_Interfere";
    }
    return "Unknown";
}

sggk::ClashMode ParseClashModeName(const std::string& mode)
{
    if (mode == "ClashExistenceOnly")
    {
        return sggk::ClashMode::ClashExistenceOnly;
    }
    if (mode == "ClashClassify")
    {
        return sggk::ClashMode::ClashClassify;
    }
    if (mode == "ClashClassifySubEntities")
    {
        return sggk::ClashMode::ClashClassifySubEntities;
    }
    throw std::runtime_error("unknown clash mode: " + mode);
}

bool ClashTypeMatches(const std::string& expected, const std::string& actual)
{
    if (expected == actual)
    {
        return true;
    }
    if (expected == "NoClash")
    {
        return actual == "Clash_None";
    }
    if (expected == "AnyClash")
    {
        return actual != "Clash_None" && actual != "Unknown";
    }
    return false;
}

std::string TopologyBriefJson(const sggk::TopologyPtr& topology)
{
    if (!topology)
    {
        return "null";
    }
    std::ostringstream os;
    os << "{\"type\":\"" << TopoTypeName(topology->TopoType())
       << "\",\"id\":" << topology->ID() << "}";
    return os.str();
}

std::string ClashPairJson(const sggk::ClashPair& pair)
{
    std::ostringstream os;
    os << "{"
       << "\"a\":" << TopologyBriefJson(pair.topoA)
       << ",\"b\":" << TopologyBriefJson(pair.topoB)
       << ",\"type\":\"" << ClashTypeName(pair.clashType) << "\""
       << "}";
    return os.str();
}

std::string TopoDistTypeName(sggk::TopoDistType type)
{
    switch (type)
    {
    case sggk::TopoDistType::Minimum: return "Minimum";
    case sggk::TopoDistType::Maximum: return "Maximum";
    case sggk::TopoDistType::Fixed: return "Fixed";
    }
    return "Unknown";
}

BBoxAggregate BodyBBoxAggregate(const sggk::BodyPtr& body)
{
    BBoxAggregate result;
    if (!body)
    {
        return result;
    }
    try
    {
        const auto box = body->CalcBndBox(true);
        if (box.IsEmpty())
        {
            return result;
        }
        result.ok = true;
        result.minX = box.MinPoint().X();
        result.minY = box.MinPoint().Y();
        result.minZ = box.MinPoint().Z();
        result.maxX = box.MaxPoint().X();
        result.maxY = box.MaxPoint().Y();
        result.maxZ = box.MaxPoint().Z();
    }
    catch (...)
    {
    }
    return result;
}

double AxisCoordinate(const sggk::Point3D& point, const std::string& axis)
{
    if (axis == "x")
    {
        return point.X();
    }
    if (axis == "y")
    {
        return point.Y();
    }
    return point.Z();
}

sggk::Dir3D AxisNormal(const std::string& axis)
{
    if (axis == "x")
    {
        return sggk::Dir3D(1.0, 0.0, 0.0);
    }
    if (axis == "y")
    {
        return sggk::Dir3D(0.0, 1.0, 0.0);
    }
    return sggk::Dir3D(0.0, 0.0, 1.0);
}

sggk::Point3D AxisPlaneOrigin(const std::string& axis, double coordinate, const BBoxAggregate& box)
{
    const double cx = box.ok ? 0.5 * (box.minX + box.maxX) : 0.0;
    const double cy = box.ok ? 0.5 * (box.minY + box.maxY) : 0.0;
    const double cz = box.ok ? 0.5 * (box.minZ + box.maxZ) : 0.0;
    if (axis == "x")
    {
        return sggk::Point3D(coordinate, cy, cz);
    }
    if (axis == "y")
    {
        return sggk::Point3D(cx, coordinate, cz);
    }
    return sggk::Point3D(cx, cy, coordinate);
}

double PlaneProbeSpan(const PlaneExtremeExpectation& check, const BBoxAggregate& box)
{
    if (check.planeSpan > 0.0)
    {
        return check.planeSpan;
    }
    double diagonal = 1.0;
    if (box.ok)
    {
        const double dx = box.maxX - box.minX;
        const double dy = box.maxY - box.minY;
        const double dz = box.maxZ - box.minZ;
        diagonal = std::sqrt(dx * dx + dy * dy + dz * dz);
    }
    return std::max({1.0, check.planeSpanScale * diagonal, 100.0 * check.tolerance});
}

sggk::FacePtr MakeCoordinatePlaneFace(
    const PlaneExtremeExpectation& check,
    double coordinate,
    const BBoxAggregate& box,
    double span)
{
    sggk::SurfacePtr plane = std::make_shared<sggk::Plane>(
        AxisPlaneOrigin(check.axis, coordinate, box),
        AxisNormal(check.axis));
    return sggk::api_create_face(
        plane,
        sggk::UVRange(sggk::Interval(-span, span), sggk::Interval(-span, span)));
}

std::string ExportDebugGeometryAsset(
    const fs::path& caseDir,
    const std::string& checkId,
    const std::string& label,
    const sggk::TopologyPtr& topology,
    std::vector<std::string>& debugGeometryRecords)
{
    if (!topology)
    {
        return "null";
    }
    const std::string stem = SanitizeFileStem(checkId + "_" + label + "_" + std::to_string(debugGeometryRecords.size() + 1));
    const fs::path path = caseDir / "debug_geometry" / (stem + ".sgt");
    SerializeTopology(topology, path);
    const std::string relative = path.lexically_relative(caseDir).generic_string();
    std::ostringstream os;
    os << "{"
       << "\"check_id\":\"" << EscapeJson(checkId) << "\""
       << ",\"label\":\"" << EscapeJson(label) << "\""
       << ",\"path\":\"" << EscapeJson(relative) << "\""
       << ",\"topology\":" << TopologyBriefJson(topology)
       << ",\"locator\":" << TopologyLocatorJson(topology)
       << "}";
    debugGeometryRecords.push_back(os.str());
    return os.str();
}

std::string DebugGeometryAssetsJson(
    const fs::path& caseDir,
    const std::string& checkId,
    const std::vector<std::pair<std::string, sggk::TopologyPtr>>& assets,
    std::vector<std::string>& debugGeometryRecords)
{
    std::ostringstream os;
    os << "[";
    bool first = true;
    for (const auto& asset : assets)
    {
        if (!asset.second)
        {
            continue;
        }
        if (!first)
        {
            os << ",";
        }
        os << ExportDebugGeometryAsset(caseDir, checkId, asset.first, asset.second, debugGeometryRecords);
        first = false;
    }
    os << "]";
    return os.str();
}

struct PlaneDistanceProbe
{
    double coordinate = 0.0;
    sggk::FacePtr planeFace;
    sggk::TopoDistRetPtr ret;
    bool success = false;
    double distance = std::numeric_limits<double>::quiet_NaN();
    std::string error;
};

PlaneDistanceProbe MeasurePlaneBodyDistance(
    const PlaneExtremeExpectation& check,
    const sggk::BodyPtr& body,
    double coordinate,
    const BBoxAggregate& box,
    double span)
{
    PlaneDistanceProbe probe;
    probe.coordinate = coordinate;
    try
    {
        probe.planeFace = MakeCoordinatePlaneFace(check, coordinate, box, span);
        if (!probe.planeFace)
        {
            probe.error = "plane_face_null";
            return probe;
        }
        probe.ret = sggk::api_topo_minimum_distance(probe.planeFace, body);
        if (!probe.ret)
        {
            probe.error = "distance_return_null";
            return probe;
        }
        probe.success = probe.ret->IsSuccess();
        if (!probe.success)
        {
            probe.error = "distance_calculation_failed";
            return probe;
        }
        probe.distance = probe.ret->Dist();
    }
    catch (const std::exception& ex)
    {
        probe.error = ex.what();
    }
    catch (...)
    {
        probe.error = "unknown plane distance exception";
    }
    return probe;
}

std::string PlaneDistanceProbeJson(
    const PlaneDistanceProbe& probe,
    const std::string& axis,
    const std::string& checkId,
    const std::string& probeLabel,
    const fs::path& caseDir,
    bool exportDebugGeometry,
    std::vector<std::string>& debugGeometryRecords)
{
    std::ostringstream os;
    os << "{"
       << "\"coordinate\":" << std::setprecision(17) << probe.coordinate
       << ",\"success\":" << (probe.success ? "true" : "false");
    if (!probe.error.empty())
    {
        os << ",\"error\":\"" << EscapeJson(probe.error) << "\"";
    }
    if (probe.success && probe.ret)
    {
        os << ",\"actual\":" << std::setprecision(17) << probe.distance
           << ",\"dist_type\":\"" << TopoDistTypeName(probe.ret->DistType()) << "\""
           << ",\"point_on_plane\":" << PointJson(probe.ret->PointOnTopo1())
           << ",\"point_on_body\":" << PointJson(probe.ret->PointOnTopo2())
           << ",\"plane_coordinate_at_contact\":" << std::setprecision(17) << AxisCoordinate(probe.ret->PointOnTopo1(), axis)
           << ",\"body_coordinate_at_contact\":" << std::setprecision(17) << AxisCoordinate(probe.ret->PointOnTopo2(), axis)
           << ",\"topology_plane\":" << TopologyBriefJson(probe.ret->Topo1())
           << ",\"topology_body\":" << TopologyBriefJson(probe.ret->Topo2());
    }
    os << ",\"debug_geometry\":[";
    bool first = true;
    auto writeAsset = [&](const std::string& label, const sggk::TopologyPtr& topology) {
        if (!exportDebugGeometry || !topology)
        {
            return;
        }
        if (!first)
        {
            os << ",";
        }
        os << ExportDebugGeometryAsset(caseDir, checkId, probeLabel + "_" + label, topology, debugGeometryRecords);
        first = false;
    };
    writeAsset("plane", probe.planeFace);
    if (probe.ret)
    {
        writeAsset("plane_topology", probe.ret->Topo1());
        writeAsset("body_topology", probe.ret->Topo2());
    }
    os << "]}";
    return os.str();
}

std::vector<std::string> EvaluatePlaneExtremeChecks(
    const CaseRecipe& recipe,
    const std::vector<sggk::BodyPtr>& resultBodies,
    const std::vector<sggk::BodyPtr>& targetBodies,
    const std::vector<sggk::BodyPtr>& toolBodies,
    const fs::path& caseDir,
    std::vector<std::string>& failures,
    std::vector<std::string>& skippedChecks,
    std::vector<std::string>& debugGeometryRecords)
{
    std::vector<std::string> records;
    for (const auto& check : recipe.expectations.planeExtremeChecks)
    {
        const auto* bodies = SelectRoleBodies(check.role, resultBodies, targetBodies, toolBodies);
        std::ostringstream record;
        record << "{"
               << "\"id\":\"" << EscapeJson(check.id) << "\""
               << ",\"role\":\"" << EscapeJson(check.role) << "\""
               << ",\"body_index\":" << check.bodyIndex
               << ",\"axis\":\"" << EscapeJson(check.axis) << "\""
               << ",\"side\":\"" << EscapeJson(check.side) << "\""
               << ",\"expected\":";
        if (check.expectedSet)
        {
            record << std::setprecision(17) << check.expected;
        }
        else
        {
            record << "null";
        }
        record << ",\"compare_expected\":" << (check.compareExpected ? "true" : "false")
               << ",\"tolerance\":" << std::setprecision(17) << check.tolerance
               << ",\"required\":" << (check.required ? "true" : "false");

        auto failOrSkip = [&](const std::string& reason) {
            record << ",\"ok\":" << (check.required ? "false" : "true")
                   << ",\"reason\":\"" << EscapeJson(reason) << "\"";
            if (check.required)
            {
                failures.push_back("plane_extreme_" + check.id + "_" + reason);
            }
            else
            {
                skippedChecks.push_back("plane_extreme_" + check.id + "_" + reason);
            }
        };

        if (!bodies)
        {
            failOrSkip("role_unavailable");
            record << "}";
            records.push_back(record.str());
            continue;
        }
        if (check.bodyIndex >= static_cast<int>(bodies->size()) || !(*bodies)[check.bodyIndex])
        {
            failOrSkip("body_unavailable");
            record << "}";
            records.push_back(record.str());
            continue;
        }

        const auto body = (*bodies)[check.bodyIndex];
        const auto box = BodyBBoxAggregate(body);
        if (!box.ok && check.planeSpan <= 0.0)
        {
            failOrSkip("bbox_for_probe_plane_unavailable");
            record << "}";
            records.push_back(record.str());
            continue;
        }
        const double defaultProbeCoordinate = check.side == "min" ? -recipe.maxModelSize : recipe.maxModelSize;
        const double probeCoordinate = check.probeCoordinateSet ? check.probeCoordinate : defaultProbeCoordinate;
        const double span = PlaneProbeSpan(check, box);
        const auto probe = MeasurePlaneBodyDistance(check, body, probeCoordinate, box, span);

        NumericExpectation exactExtreme;
        exactExtreme.expectedSet = true;
        exactExtreme.expectedValue = check.expected;
        exactExtreme.absTol = check.tolerance;
        exactExtreme.relTol = 0.0;

        std::vector<std::string> metricFailures;
        double actualExtreme = std::numeric_limits<double>::quiet_NaN();
        if (!probe.success)
        {
            metricFailures.push_back("plane_extreme_" + check.id + "_distance_failed");
        }
        else if (check.compareExpected)
        {
            actualExtreme = check.side == "min"
                ? probeCoordinate + probe.distance
                : probeCoordinate - probe.distance;
            AddMetricExpectationFailures(
                "plane_extreme_" + check.id,
                actualExtreme,
                exactExtreme,
                metricFailures);
        }
        else
        {
            actualExtreme = check.side == "min"
                ? probeCoordinate + probe.distance
                : probeCoordinate - probe.distance;
        }

        const bool ok = metricFailures.empty();
        const bool exportDebug = check.exportDebugGeometry && !ok;
        record << ",\"probe_coordinate\":" << std::setprecision(17) << probeCoordinate
               << ",\"probe_coordinate_source\":\"" << (check.probeCoordinateSet ? "explicit" : "max_model_size") << "\""
               << ",\"max_model_size\":" << std::setprecision(17) << recipe.maxModelSize
               << ",\"actual_extreme\":" << std::setprecision(17) << actualExtreme
               << ",\"plane_span\":" << std::setprecision(17) << span
               << ",\"bbox_for_probe\":" << (box.ok ? "true" : "false")
               << ",\"probe\":" << PlaneDistanceProbeJson(
                   probe,
                   check.axis,
                   check.id,
                   "probe",
                   caseDir,
                   exportDebug,
                   debugGeometryRecords)
               << ",\"debug_geometry\":[";
        if (exportDebug)
        {
            record << ExportDebugGeometryAsset(caseDir, check.id, "body", body, debugGeometryRecords);
        }
        record << "]"
               << ",\"metric_failures\":[";
        for (size_t i = 0; i < metricFailures.size(); ++i)
        {
            if (i != 0)
            {
                record << ",";
            }
            record << "\"" << EscapeJson(metricFailures[i]) << "\"";
        }
        record << "]"
               << ",\"ok\":" << (ok ? "true" : "false");
        if (!ok)
        {
            for (const auto& failure : metricFailures)
            {
                if (check.required)
                {
                    failures.push_back(failure);
                }
                else
                {
                    skippedChecks.push_back(failure);
                }
            }
        }
        record << "}";
        records.push_back(record.str());
    }
    return records;
}

std::string BodyPtInfoTargetJson(const sggk::BodyPtInfo& info)
{
    std::ostringstream os;
    os << "{";
    bool wrote = false;
    auto writeTarget = [&](const std::string& key, const sggk::TopologyPtr& topology) {
        if (!topology)
        {
            return;
        }
        if (wrote)
        {
            os << ",";
        }
        os << "\"" << key << "\":{\"type\":\"" << TopoTypeName(topology->TopoType())
           << "\",\"id\":" << topology->ID() << "}";
        wrote = true;
    };
    writeTarget("vertex", info.targetVertex);
    writeTarget("edge", info.targetEdge);
    writeTarget("face", info.targetFace);
    os << "}";
    return os.str();
}

std::vector<std::string> EvaluatePointRelations(
    const CaseRecipe& recipe,
    const std::vector<sggk::BodyPtr>& resultBodies,
    const std::vector<sggk::BodyPtr>& targetBodies,
    const std::vector<sggk::BodyPtr>& toolBodies,
    const fs::path& caseDir,
    std::vector<std::string>& failures,
    std::vector<std::string>& skippedChecks,
    std::vector<std::string>& debugGeometryRecords)
{
    std::vector<std::string> records;
    for (const auto& relation : recipe.expectations.pointRelations)
    {
        const auto* bodies = SelectRoleBodies(relation.role, resultBodies, targetBodies, toolBodies);
        std::ostringstream record;
        record << "{"
               << "\"id\":\"" << EscapeJson(relation.id) << "\""
               << ",\"role\":\"" << EscapeJson(relation.role) << "\""
               << ",\"body_index\":" << relation.bodyIndex
               << ",\"point_ref\":\"" << EscapeJson(relation.pointRef) << "\""
               << ",\"point\":[" << std::setprecision(17) << relation.x
               << "," << std::setprecision(17) << relation.y
               << "," << std::setprecision(17) << relation.z << "]"
               << ",\"expected\":\"" << EscapeJson(relation.expected) << "\""
               << ",\"tolerance\":" << std::setprecision(17) << relation.tolerance
               << ",\"check_boundary\":" << (relation.checkBoundary ? "true" : "false")
               << ",\"required\":" << (relation.required ? "true" : "false");

        auto failOrSkip = [&](const std::string& reason) {
            record << ",\"ok\":" << (relation.required ? "false" : "true")
                   << ",\"reason\":\"" << EscapeJson(reason) << "\"";
            if (relation.required)
            {
                failures.push_back("point_relation_" + relation.id + "_" + reason);
            }
            else
            {
                skippedChecks.push_back("point_relation_" + relation.id + "_" + reason);
            }
        };

        if (!bodies)
        {
            failOrSkip("role_unavailable");
            record << "}";
            records.push_back(record.str());
            continue;
        }
        if (relation.bodyIndex >= static_cast<int>(bodies->size()) || !(*bodies)[relation.bodyIndex])
        {
            failOrSkip("body_unavailable");
            record << "}";
            records.push_back(record.str());
            continue;
        }

        const auto body = (*bodies)[relation.bodyIndex];
        try
        {
            sggk::PtBodyRelation evaluator(body);
            const auto info = evaluator.Execute(
                sggk::Point3D(relation.x, relation.y, relation.z),
                sggk::Toler(relation.tolerance),
                relation.checkBoundary);
            const std::string actual = BodyPtRelTypeName(info.relation);
            const bool ok = BodyPtRelationMatches(relation.expected, actual);
            record << ",\"actual\":\"" << EscapeJson(actual) << "\""
                   << ",\"ok\":" << (ok ? "true" : "false")
                   << ",\"target\":" << BodyPtInfoTargetJson(info);
            if (!ok)
            {
                record << ",\"debug_geometry\":" << DebugGeometryAssetsJson(
                    caseDir,
                    "point_relation_" + relation.id,
                    {
                        {"body", body},
                        {"target_vertex", info.targetVertex},
                        {"target_edge", info.targetEdge},
                        {"target_face", info.targetFace},
                    },
                    debugGeometryRecords);
                std::ostringstream failure;
                failure << "point_relation_" << relation.id
                        << "_mismatch expected=" << relation.expected
                        << " actual=" << actual;
                if (relation.required)
                {
                    failures.push_back(failure.str());
                }
                else
                {
                    skippedChecks.push_back(failure.str());
                }
            }
        }
        catch (const std::exception& ex)
        {
            record << ",\"ok\":false,\"error\":\"" << EscapeJson(ex.what()) << "\""
                   << ",\"debug_geometry\":" << DebugGeometryAssetsJson(
                       caseDir,
                       "point_relation_" + relation.id,
                       {{"body", body}},
                       debugGeometryRecords);
            if (relation.required)
            {
                failures.push_back("point_relation_" + relation.id + "_exception");
            }
            else
            {
                skippedChecks.push_back("point_relation_" + relation.id + "_exception");
            }
        }
        catch (...)
        {
            record << ",\"ok\":false,\"error\":\"unknown point relation exception\""
                   << ",\"debug_geometry\":" << DebugGeometryAssetsJson(
                       caseDir,
                       "point_relation_" + relation.id,
                       {{"body", body}},
                       debugGeometryRecords);
            if (relation.required)
            {
                failures.push_back("point_relation_" + relation.id + "_exception");
            }
            else
            {
                skippedChecks.push_back("point_relation_" + relation.id + "_exception");
            }
        }
        record << "}";
        records.push_back(record.str());
    }
    return records;
}

std::vector<std::string> EvaluateFacePointRelations(
    const CaseRecipe& recipe,
    const std::vector<sggk::BodyPtr>& resultBodies,
    const std::vector<sggk::BodyPtr>& targetBodies,
    const std::vector<sggk::BodyPtr>& toolBodies,
    const fs::path& caseDir,
    std::vector<std::string>& failures,
    std::vector<std::string>& skippedChecks,
    std::vector<std::string>& debugGeometryRecords)
{
    std::vector<std::string> records;
    for (const auto& relation : recipe.expectations.facePointRelations)
    {
        const auto* bodies = SelectRoleBodies(relation.role, resultBodies, targetBodies, toolBodies);
        std::ostringstream record;
        record << "{"
               << "\"id\":\"" << EscapeJson(relation.id) << "\""
               << ",\"role\":\"" << EscapeJson(relation.role) << "\""
               << ",\"body_index\":" << relation.bodyIndex
               << ",\"face_index\":" << relation.faceIndex
               << ",\"face_id_set\":" << (relation.useFaceId ? "true" : "false")
               << ",\"face_id\":" << relation.faceId
               << ",\"expected\":\"" << EscapeJson(relation.expected) << "\""
               << ",\"tolerance\":" << std::setprecision(17) << relation.tolerance
               << ",\"check_boundary\":" << (relation.checkBoundary ? "true" : "false")
               << ",\"required\":" << (relation.required ? "true" : "false");

        auto failOrSkip = [&](const std::string& reason) {
            record << ",\"ok\":" << (relation.required ? "false" : "true")
                   << ",\"reason\":\"" << EscapeJson(reason) << "\"";
            if (relation.required)
            {
                failures.push_back("face_point_relation_" + relation.id + "_" + reason);
            }
            else
            {
                skippedChecks.push_back("face_point_relation_" + relation.id + "_" + reason);
            }
        };

        if (!bodies)
        {
            failOrSkip("role_unavailable");
            record << "}";
            records.push_back(record.str());
            continue;
        }
        if (relation.bodyIndex >= static_cast<int>(bodies->size()) || !(*bodies)[relation.bodyIndex])
        {
            failOrSkip("body_unavailable");
            record << "}";
            records.push_back(record.str());
            continue;
        }

        const auto body = (*bodies)[relation.bodyIndex];
        const auto face = SelectFace(body, relation.faceIndex, relation.faceId, relation.useFaceId);
        if (!face)
        {
            failOrSkip("face_unavailable");
            record << "}";
            records.push_back(record.str());
            continue;
        }

        try
        {
            record << ",\"actual_face\":{\"type\":\"Face\",\"id\":" << face->ID() << "}";
            const auto& uvBound = face->CalcUVBound();
            record << ",\"uv_bound\":" << UVRangeJson(uvBound);

            bool usedUv = relation.hasUv || relation.hasUvFraction;
            sggk::Point2D uv(relation.u, relation.v);
            if (relation.hasUvFraction)
            {
                uv = UVFromFraction(uvBound, relation.uFraction, relation.vFraction);
            }
            bool havePoint = relation.hasPoint;
            sggk::Point3D point(relation.x, relation.y, relation.z);
            bool pointFromSurface = false;
            if (usedUv && !havePoint)
            {
                const auto surface = face->GeomSurface();
                if (!surface)
                {
                    failOrSkip("surface_unavailable");
                    record << "}";
                    records.push_back(record.str());
                    continue;
                }
                point = surface->CalcPoint(uv);
                havePoint = true;
                pointFromSurface = true;
            }

            sggk::FacePtRelInfo info;
            if (usedUv && relation.hasPoint)
            {
                info = sggk::FacePtRelation::Perform(
                    face,
                    point,
                    uv,
                    sggk::Toler(relation.tolerance),
                    relation.checkBoundary);
            }
            else if (usedUv)
            {
                info = sggk::FacePtRelation::Perform(
                    face,
                    uv,
                    sggk::Toler(relation.tolerance),
                    relation.checkBoundary);
            }
            else if (relation.hasPoint)
            {
                info = sggk::FacePtRelation::Perform(
                    face,
                    point,
                    sggk::Toler(relation.tolerance),
                    relation.checkBoundary);
            }
            else
            {
                failOrSkip("point_or_uv_unavailable");
                record << "}";
                records.push_back(record.str());
                continue;
            }

            const std::string actual = FacePtRelTypeName(info.relation);
            const bool ok = FacePtRelationMatches(relation.expected, actual);
            record << ",\"uv\":" << (usedUv ? Point2DJson(uv) : "null")
                   << ",\"point\":" << (havePoint ? PointJson(point) : "null")
                   << ",\"point_from_surface\":" << (pointFromSurface ? "true" : "false")
                   << ",\"actual\":\"" << EscapeJson(actual) << "\""
                   << ",\"ok\":" << (ok ? "true" : "false")
                   << ",\"target\":" << FacePtInfoTargetJson(info);
            if (!ok)
            {
                record << ",\"debug_geometry\":" << DebugGeometryAssetsJson(
                    caseDir,
                    "face_point_relation_" + relation.id,
                    {
                        {"body", body},
                        {"selected_face", face},
                        {"target_vertex", info.targetVertex},
                        {"target_edge", info.targetEdge},
                    },
                    debugGeometryRecords);
                std::ostringstream failure;
                failure << "face_point_relation_" << relation.id
                        << "_mismatch expected=" << relation.expected
                        << " actual=" << actual;
                if (relation.required)
                {
                    failures.push_back(failure.str());
                }
                else
                {
                    skippedChecks.push_back(failure.str());
                }
            }
        }
        catch (const std::exception& ex)
        {
            record << ",\"ok\":false,\"error\":\"" << EscapeJson(ex.what()) << "\""
                   << ",\"debug_geometry\":" << DebugGeometryAssetsJson(
                       caseDir,
                       "face_point_relation_" + relation.id,
                       {{"body", body}, {"selected_face", face}},
                       debugGeometryRecords);
            if (relation.required)
            {
                failures.push_back("face_point_relation_" + relation.id + "_exception");
            }
            else
            {
                skippedChecks.push_back("face_point_relation_" + relation.id + "_exception");
            }
        }
        catch (...)
        {
            record << ",\"ok\":false,\"error\":\"unknown face point relation exception\""
                   << ",\"debug_geometry\":" << DebugGeometryAssetsJson(
                       caseDir,
                       "face_point_relation_" + relation.id,
                       {{"body", body}, {"selected_face", face}},
                       debugGeometryRecords);
            if (relation.required)
            {
                failures.push_back("face_point_relation_" + relation.id + "_exception");
            }
            else
            {
                skippedChecks.push_back("face_point_relation_" + relation.id + "_exception");
            }
        }
        record << "}";
        records.push_back(record.str());
    }
    return records;
}

std::vector<std::string> EvaluateClashChecks(
    const CaseRecipe& recipe,
    const std::vector<sggk::BodyPtr>& resultBodies,
    const std::vector<sggk::BodyPtr>& targetBodies,
    const std::vector<sggk::BodyPtr>& toolBodies,
    const fs::path& caseDir,
    std::vector<std::string>& failures,
    std::vector<std::string>& skippedChecks,
    std::vector<std::string>& debugGeometryRecords)
{
    std::vector<std::string> records;
    for (const auto& check : recipe.expectations.clashChecks)
    {
        const auto* bodiesA = SelectRoleBodies(check.roleA, resultBodies, targetBodies, toolBodies);
        const auto* bodiesB = SelectRoleBodies(check.roleB, resultBodies, targetBodies, toolBodies);
        std::ostringstream record;
        record << "{"
               << "\"id\":\"" << EscapeJson(check.id) << "\""
               << ",\"role_a\":\"" << EscapeJson(check.roleA) << "\""
               << ",\"role_b\":\"" << EscapeJson(check.roleB) << "\""
               << ",\"body_index_a\":" << check.bodyIndexA
               << ",\"body_index_b\":" << check.bodyIndexB
               << ",\"expected\":\"" << EscapeJson(check.expected) << "\""
               << ",\"mode\":\"" << EscapeJson(check.mode) << "\""
               << ",\"tolerance\":" << std::setprecision(17) << check.tolerance
               << ",\"required\":" << (check.required ? "true" : "false");

        auto failOrSkip = [&](const std::string& reason) {
            record << ",\"ok\":" << (check.required ? "false" : "true")
                   << ",\"reason\":\"" << EscapeJson(reason) << "\"";
            if (check.required)
            {
                failures.push_back("clash_check_" + check.id + "_" + reason);
            }
            else
            {
                skippedChecks.push_back("clash_check_" + check.id + "_" + reason);
            }
        };

        if (!bodiesA || !bodiesB)
        {
            failOrSkip("role_unavailable");
            record << "}";
            records.push_back(record.str());
            continue;
        }
        if (check.bodyIndexA >= static_cast<int>(bodiesA->size()) || !(*bodiesA)[check.bodyIndexA] ||
            check.bodyIndexB >= static_cast<int>(bodiesB->size()) || !(*bodiesB)[check.bodyIndexB])
        {
            failOrSkip("body_unavailable");
            record << "}";
            records.push_back(record.str());
            continue;
        }

        const auto bodyA = (*bodiesA)[check.bodyIndexA];
        const auto bodyB = (*bodiesB)[check.bodyIndexB];
        try
        {
            const auto ret = sggk::api_body_clash(
                bodyA,
                bodyB,
                sggk::ClashOpts(ParseClashModeName(check.mode), check.tolerance));
            if (!ret)
            {
                failOrSkip("null_return");
                record << ",\"debug_geometry\":" << DebugGeometryAssetsJson(
                    caseDir,
                    "clash_check_" + check.id,
                    {{"body_a", bodyA}, {"body_b", bodyB}},
                    debugGeometryRecords);
                record << "}";
                records.push_back(record.str());
                continue;
            }

            const std::string actual = ClashTypeName(ret->GetClashType());
            const auto& subPairs = ret->GetSubClashPairs();
            const bool ok = ClashTypeMatches(check.expected, actual);
            record << ",\"actual\":\"" << EscapeJson(actual) << "\""
                   << ",\"ok\":" << (ok ? "true" : "false")
                   << ",\"sub_clash_count\":" << subPairs.size()
                   << ",\"sub_clashes\":[";
            size_t written = 0;
            for (const auto& pair : subPairs)
            {
                if (written >= 8)
                {
                    break;
                }
                if (written != 0)
                {
                    record << ",";
                }
                record << ClashPairJson(pair);
                ++written;
            }
            record << "]";
            if (!ok)
            {
                std::vector<std::pair<std::string, sggk::TopologyPtr>> assets = {
                    {"body_a", bodyA},
                    {"body_b", bodyB},
                };
                size_t assetIndex = 0;
                for (const auto& pair : subPairs)
                {
                    if (assetIndex >= 4)
                    {
                        break;
                    }
                    assets.push_back({"sub_" + std::to_string(assetIndex + 1) + "_a", pair.topoA});
                    assets.push_back({"sub_" + std::to_string(assetIndex + 1) + "_b", pair.topoB});
                    ++assetIndex;
                }
                record << ",\"debug_geometry\":" << DebugGeometryAssetsJson(
                    caseDir,
                    "clash_check_" + check.id,
                    assets,
                    debugGeometryRecords);
                std::ostringstream failure;
                failure << "clash_check_" << check.id
                        << "_mismatch expected=" << check.expected
                        << " actual=" << actual;
                if (check.required)
                {
                    failures.push_back(failure.str());
                }
                else
                {
                    skippedChecks.push_back(failure.str());
                }
            }
        }
        catch (const std::exception& ex)
        {
            record << ",\"ok\":false,\"error\":\"" << EscapeJson(ex.what()) << "\""
                   << ",\"debug_geometry\":" << DebugGeometryAssetsJson(
                       caseDir,
                       "clash_check_" + check.id,
                       {{"body_a", bodyA}, {"body_b", bodyB}},
                       debugGeometryRecords);
            if (check.required)
            {
                failures.push_back("clash_check_" + check.id + "_exception");
            }
            else
            {
                skippedChecks.push_back("clash_check_" + check.id + "_exception");
            }
        }
        catch (...)
        {
            record << ",\"ok\":false,\"error\":\"unknown clash exception\""
                   << ",\"debug_geometry\":" << DebugGeometryAssetsJson(
                       caseDir,
                       "clash_check_" + check.id,
                       {{"body_a", bodyA}, {"body_b", bodyB}},
                       debugGeometryRecords);
            if (check.required)
            {
                failures.push_back("clash_check_" + check.id + "_exception");
            }
            else
            {
                skippedChecks.push_back("clash_check_" + check.id + "_exception");
            }
        }
        record << "}";
        records.push_back(record.str());
    }
    return records;
}

std::vector<std::string> EvaluateDistanceChecks(
    const CaseRecipe& recipe,
    const std::vector<sggk::BodyPtr>& resultBodies,
    const std::vector<sggk::BodyPtr>& targetBodies,
    const std::vector<sggk::BodyPtr>& toolBodies,
    const fs::path& caseDir,
    std::vector<std::string>& failures,
    std::vector<std::string>& skippedChecks,
    std::vector<std::string>& debugGeometryRecords)
{
    std::vector<std::string> records;
    for (const auto& check : recipe.expectations.distanceChecks)
    {
        const auto* bodiesA = SelectRoleBodies(check.roleA, resultBodies, targetBodies, toolBodies);
        const auto* bodiesB = SelectRoleBodies(check.roleB, resultBodies, targetBodies, toolBodies);
        std::ostringstream record;
        record << "{"
               << "\"id\":\"" << EscapeJson(check.id) << "\""
               << ",\"role_a\":\"" << EscapeJson(check.roleA) << "\""
               << ",\"role_b\":\"" << EscapeJson(check.roleB) << "\""
               << ",\"body_index_a\":" << check.bodyIndexA
               << ",\"body_index_b\":" << check.bodyIndexB
               << ",\"kind\":\"" << EscapeJson(check.kind) << "\""
               << ",\"threshold\":" << std::setprecision(17) << check.threshold
               << ",\"expectation\":" << NumericExpectationJson(check.distance)
               << ",\"required\":" << (check.required ? "true" : "false");

        auto failOrSkip = [&](const std::string& reason) {
            record << ",\"ok\":" << (check.required ? "false" : "true")
                   << ",\"reason\":\"" << EscapeJson(reason) << "\"";
            if (check.required)
            {
                failures.push_back("distance_check_" + check.id + "_" + reason);
            }
            else
            {
                skippedChecks.push_back("distance_check_" + check.id + "_" + reason);
            }
        };

        if (!bodiesA || !bodiesB)
        {
            failOrSkip("role_unavailable");
            record << "}";
            records.push_back(record.str());
            continue;
        }
        if (check.bodyIndexA >= static_cast<int>(bodiesA->size()) || !(*bodiesA)[check.bodyIndexA] ||
            check.bodyIndexB >= static_cast<int>(bodiesB->size()) || !(*bodiesB)[check.bodyIndexB])
        {
            failOrSkip("body_unavailable");
            record << "}";
            records.push_back(record.str());
            continue;
        }

        const auto bodyA = (*bodiesA)[check.bodyIndexA];
        const auto bodyB = (*bodiesB)[check.bodyIndexB];
        try
        {
            sggk::TopoDistRetPtr ret;
            if (check.kind == "minimum")
            {
                if (check.threshold > 0.0)
                {
                    ret = sggk::api_topo_minimum_distance(
                        bodyA,
                        bodyB,
                        check.threshold);
                }
                else
                {
                    ret = sggk::api_topo_minimum_distance(
                        bodyA,
                        bodyB);
                }
            }
            else if (check.kind == "maximum")
            {
                ret = sggk::api_topo_maximum_distance(
                    bodyA,
                    bodyB);
            }
            else
            {
                throw std::runtime_error("unknown distance kind: " + check.kind);
            }

            if (!ret)
            {
                failOrSkip("null_return");
                record << ",\"debug_geometry\":" << DebugGeometryAssetsJson(
                    caseDir,
                    "distance_check_" + check.id,
                    {{"body_a", bodyA}, {"body_b", bodyB}},
                    debugGeometryRecords);
                record << "}";
                records.push_back(record.str());
                continue;
            }

            const bool success = ret->IsSuccess();
            record << ",\"success\":" << (success ? "true" : "false")
                   << ",\"dist_type\":\"" << TopoDistTypeName(ret->DistType()) << "\"";
            if (!success)
            {
                failOrSkip("calculation_failed");
                record << ",\"debug_geometry\":" << DebugGeometryAssetsJson(
                    caseDir,
                    "distance_check_" + check.id,
                    {{"body_a", bodyA}, {"body_b", bodyB}},
                    debugGeometryRecords);
                record << "}";
                records.push_back(record.str());
                continue;
            }

            const double actual = ret->Dist();
            std::vector<std::string> metricFailures;
            AddMetricExpectationFailures("distance_check_" + check.id, actual, check.distance, metricFailures);
            const bool ok = metricFailures.empty();
            record << ",\"actual\":" << std::setprecision(17) << actual
                   << ",\"ok\":" << (ok ? "true" : "false")
                   << ",\"point_a\":" << PointJson(ret->PointOnTopo1())
                   << ",\"point_b\":" << PointJson(ret->PointOnTopo2())
                   << ",\"topology_a\":" << TopologyBriefJson(ret->Topo1())
                   << ",\"topology_b\":" << TopologyBriefJson(ret->Topo2())
                   << ",\"metric_failures\":[";
            for (size_t i = 0; i < metricFailures.size(); ++i)
            {
                if (i != 0)
                {
                    record << ",";
                }
                record << "\"" << EscapeJson(metricFailures[i]) << "\"";
            }
            record << "]";
            if (!ok)
            {
                record << ",\"debug_geometry\":" << DebugGeometryAssetsJson(
                    caseDir,
                    "distance_check_" + check.id,
                    {
                        {"body_a", bodyA},
                        {"body_b", bodyB},
                        {"topology_a", ret->Topo1()},
                        {"topology_b", ret->Topo2()},
                    },
                    debugGeometryRecords);
                for (const auto& failure : metricFailures)
                {
                    if (check.required)
                    {
                        failures.push_back(failure);
                    }
                    else
                    {
                        skippedChecks.push_back(failure);
                    }
                }
            }
        }
        catch (const std::exception& ex)
        {
            record << ",\"ok\":false,\"error\":\"" << EscapeJson(ex.what()) << "\""
                   << ",\"debug_geometry\":" << DebugGeometryAssetsJson(
                       caseDir,
                       "distance_check_" + check.id,
                       {{"body_a", bodyA}, {"body_b", bodyB}},
                       debugGeometryRecords);
            if (check.required)
            {
                failures.push_back("distance_check_" + check.id + "_exception");
            }
            else
            {
                skippedChecks.push_back("distance_check_" + check.id + "_exception");
            }
        }
        catch (...)
        {
            record << ",\"ok\":false,\"error\":\"unknown distance exception\""
                   << ",\"debug_geometry\":" << DebugGeometryAssetsJson(
                       caseDir,
                       "distance_check_" + check.id,
                       {{"body_a", bodyA}, {"body_b", bodyB}},
                       debugGeometryRecords);
            if (check.required)
            {
                failures.push_back("distance_check_" + check.id + "_exception");
            }
            else
            {
                skippedChecks.push_back("distance_check_" + check.id + "_exception");
            }
        }
        record << "}";
        records.push_back(record.str());
    }
    return records;
}

bool WriteValidation(
    const CaseRecipe& recipe,
    const std::vector<sggk::BodyPtr>& resultBodies,
    const std::vector<BodyProperties>& resultProperties,
    const std::vector<sggk::BodyPtr>& targetBodies,
    const std::vector<BodyProperties>& targetProperties,
    const std::vector<sggk::BodyPtr>& toolBodies,
    const std::vector<BodyProperties>& toolProperties,
    const fs::path& caseDir,
    const std::vector<std::string>& extraFailures = {},
    const std::string& apiSpecificJson = "{}")
{
    std::vector<std::string> failures = extraFailures;
    std::vector<std::string> skippedChecks;
    std::vector<std::string> debugGeometryRecords;
    const auto& expectations = recipe.expectations;
    const int resultCount = static_cast<int>(resultProperties.size());
    if (resultCount < expectations.minResultBodies)
    {
        std::ostringstream os;
        os << "result_body_count_below_min actual=" << resultCount
           << " min=" << expectations.minResultBodies;
        failures.push_back(os.str());
    }
    if (expectations.maxResultBodiesSet && resultCount > expectations.maxResultBodies)
    {
        std::ostringstream os;
        os << "result_body_count_above_max actual=" << resultCount
           << " max=" << expectations.maxResultBodies;
        failures.push_back(os.str());
    }

    for (const auto& property : resultProperties)
    {
        if (!property.propertyOk)
        {
            if (expectations.requirePropertyCalculations)
            {
                failures.push_back("body_" + std::to_string(property.index) + "_property_error: " + property.propertyError);
            }
            continue;
        }
        if (expectations.requireFiniteProperties &&
            (!std::isfinite(property.length) || !std::isfinite(property.area) || !std::isfinite(property.volume)))
        {
            failures.push_back("body_" + std::to_string(property.index) + "_nonfinite_property");
        }
        if (expectations.requireNonnegativeLengthArea)
        {
            if (property.length < -expectations.relationAbsTol)
            {
                failures.push_back("body_" + std::to_string(property.index) + "_negative_length");
            }
            if (property.area < -expectations.relationAbsTol)
            {
                failures.push_back("body_" + std::to_string(property.index) + "_negative_area");
            }
        }
        if (expectations.requireNonnegativeVolume && property.volume < -expectations.relationAbsTol)
        {
            failures.push_back("body_" + std::to_string(property.index) + "_negative_volume");
        }
    }

    const double totalLength = TotalMetric(resultProperties, &BodyProperties::length);
    const double totalArea = TotalMetric(resultProperties, &BodyProperties::area);
    const double totalVolume = TotalMetric(resultProperties, &BodyProperties::volume);
    const double totalAbsVolume = TotalAbsVolume(resultProperties);
    AddMetricExpectationFailures("total_length", totalLength, expectations.totalLength, failures);
    AddMetricExpectationFailures("total_area", totalArea, expectations.totalArea, failures);
    AddMetricExpectationFailures("total_volume", totalVolume, expectations.totalVolume, failures);
    AddMetricExpectationFailures("total_abs_volume", totalAbsVolume, expectations.totalAbsVolume, failures);

    const double targetAbsVolume = TotalAbsVolume(targetProperties);
    const double toolAbsVolume = TotalAbsVolume(toolProperties);
    if (recipe.api == "api_boolean" && expectations.booleanVolumeRelation)
    {
        if (AllPropertiesOk(targetProperties) && AllPropertiesOk(toolProperties))
        {
            AddBooleanVolumeRelationFailures(recipe, targetAbsVolume, toolAbsVolume, totalAbsVolume, failures);
        }
        else
        {
            if (expectations.sampleInputProperties)
            {
                failures.push_back("boolean_volume_relation_input_property_unavailable");
            }
            else
            {
                skippedChecks.push_back("boolean_volume_relation_skipped_missing_input_properties");
            }
        }
    }
    if (recipe.api == "api_boolean" && expectations.booleanBboxRelation)
    {
        AddBooleanBBoxRelationDiagnostics(recipe, resultProperties, targetProperties, toolProperties, skippedChecks);
    }
    const auto pointRelationRecords = EvaluatePointRelations(
        recipe,
        resultBodies,
        targetBodies,
        toolBodies,
        caseDir,
        failures,
        skippedChecks,
        debugGeometryRecords);
    const auto facePointRelationRecords = EvaluateFacePointRelations(
        recipe,
        resultBodies,
        targetBodies,
        toolBodies,
        caseDir,
        failures,
        skippedChecks,
        debugGeometryRecords);
    const auto clashCheckRecords = EvaluateClashChecks(
        recipe,
        resultBodies,
        targetBodies,
        toolBodies,
        caseDir,
        failures,
        skippedChecks,
        debugGeometryRecords);
    const auto distanceCheckRecords = EvaluateDistanceChecks(
        recipe,
        resultBodies,
        targetBodies,
        toolBodies,
        caseDir,
        failures,
        skippedChecks,
        debugGeometryRecords);
    const auto planeExtremeCheckRecords = EvaluatePlaneExtremeChecks(
        recipe,
        resultBodies,
        targetBodies,
        toolBodies,
        caseDir,
        failures,
        skippedChecks,
        debugGeometryRecords);

    std::ostringstream os;
    os << "{\n"
       << "  \"ok\": " << (failures.empty() ? "true" : "false") << ",\n"
       << "  \"expectations\": " << ValidationExpectationsJson(expectations) << ",\n"
       << "  \"api_specific\": " << apiSpecificJson << ",\n"
       << "  \"result_body_count\": " << resultCount << ",\n"
       << "  \"totals\": {\n"
       << "    \"length\": " << std::setprecision(17) << totalLength << ",\n"
       << "    \"area\": " << std::setprecision(17) << totalArea << ",\n"
       << "    \"volume\": " << std::setprecision(17) << totalVolume << ",\n"
       << "    \"abs_volume\": " << std::setprecision(17) << totalAbsVolume << "\n"
       << "  },\n"
       << "  \"input_totals\": {\n"
       << "    \"target_abs_volume\": " << std::setprecision(17) << targetAbsVolume << ",\n"
       << "    \"tool_abs_volume\": " << std::setprecision(17) << toolAbsVolume << "\n"
       << "  },\n"
       << "  \"point_relations\": [";
    for (size_t i = 0; i < pointRelationRecords.size(); ++i)
    {
        if (i != 0)
        {
            os << ",";
        }
        os << pointRelationRecords[i];
    }
    os << "],\n"
       << "  \"face_point_relations\": [";
    for (size_t i = 0; i < facePointRelationRecords.size(); ++i)
    {
        if (i != 0)
        {
            os << ",";
        }
        os << facePointRelationRecords[i];
    }
    os << "],\n"
       << "  \"clash_checks\": [";
    for (size_t i = 0; i < clashCheckRecords.size(); ++i)
    {
        if (i != 0)
        {
            os << ",";
        }
        os << clashCheckRecords[i];
    }
    os << "],\n"
       << "  \"distance_checks\": [";
    for (size_t i = 0; i < distanceCheckRecords.size(); ++i)
    {
        if (i != 0)
        {
            os << ",";
        }
        os << distanceCheckRecords[i];
    }
    os << "],\n"
       << "  \"plane_extreme_checks\": [";
    for (size_t i = 0; i < planeExtremeCheckRecords.size(); ++i)
    {
        if (i != 0)
        {
            os << ",";
        }
        os << planeExtremeCheckRecords[i];
    }
    os << "],\n"
       << "  \"failures\": [";
    for (size_t i = 0; i < failures.size(); ++i)
    {
        if (i != 0)
        {
            os << ",";
        }
        os << "\"" << EscapeJson(failures[i]) << "\"";
    }
    os << "],\n"
       << "  \"skipped_checks\": [";
    for (size_t i = 0; i < skippedChecks.size(); ++i)
    {
        if (i != 0)
        {
            os << ",";
        }
        os << "\"" << EscapeJson(skippedChecks[i]) << "\"";
    }
    os << "]\n"
       << "}\n";
    WriteTextFile(caseDir / "report" / "validation.json", os.str());

    std::ostringstream debugIndex;
    debugIndex << "{\n"
               << "  \"case_id\": \"" << EscapeJson(recipe.caseId) << "\",\n"
               << "  \"assets\": [";
    for (size_t i = 0; i < debugGeometryRecords.size(); ++i)
    {
        if (i != 0)
        {
            debugIndex << ",";
        }
        debugIndex << debugGeometryRecords[i];
    }
    debugIndex << "]\n"
               << "}\n";
    WriteTextFile(caseDir / "report" / "debug_geometry_index.json", debugIndex.str());
    return failures.empty();
}

void WriteTopoTrack(
    const CaseRecipe& recipe,
    const sggk::ModelingRetPtr& ret,
    const InputTopologyIndex& inputIndex,
    const fs::path& caseDir)
{
    std::ostringstream os;
    os << "{\n"
       << "  \"dsl\": " << DslProvenanceJson(recipe, 4) << ",\n"
       << "  \"input_operations\": {\n"
       << "    \"target\": " << StringArrayJson(recipe.boolean.target.operations) << ",\n"
       << "    \"tool\": " << StringArrayJson(recipe.boolean.tool.operations) << "\n"
       << "  },\n"
       << "  \"items\": [\n";
    bool firstItem = true;
    for (const auto& itemPtr : ret->QueryTopoTrackItems())
    {
        if (!itemPtr || !itemPtr->descendent)
        {
            continue;
        }
        if (!firstItem)
        {
            os << ",\n";
        }
        firstItem = false;
        os << "    {\"descendent\":" << TopologyEntityJson(itemPtr->descendent)
           << ",\"track_type\":\"" << TrackTypeName(itemPtr->trackType) << "\""
           << ",\"ancestors\":[";

        bool firstAncestor = true;
        for (const auto& ancestor : itemPtr->ancestors)
        {
            const auto entity = std::dynamic_pointer_cast<sggk::Entity>(ancestor);
            if (!entity)
            {
                continue;
            }
            const auto topo = sggk::Entity::Cast<sggk::Topology>(entity);
            if (!firstAncestor)
            {
                os << ",";
            }
            firstAncestor = false;
            os << "{\"id\":" << entity->ID();
            if (topo)
            {
                os << ",\"type\":\"" << TopoTypeName(topo->TopoType()) << "\"";
                bool ambiguous = false;
                const auto* inputRef = FindInputTopologyRef(inputIndex, topo, ambiguous);
                if (inputRef)
                {
                    os << ",\"input_ref\":" << InputRefJson(*inputRef);
                }
                else if (ambiguous)
                {
                    os << ",\"input_ref_status\":\"ambiguous\"";
                }
                else
                {
                    os << ",\"input_ref_status\":\"unresolved\"";
                }
            }
            else
            {
                os << ",\"input_ref_status\":\"non_topology_entity\"";
            }
            os << "}";
        }
        os << "]}";
    }
    os << "\n  ]\n}\n";
    WriteTextFile(caseDir / "report" / "topo_track.json", os.str());
}

std::string StringIntMapJson(const std::map<std::string, int>& values)
{
    std::ostringstream os;
    os << "{";
    bool first = true;
    for (const auto& item : values)
    {
        if (!first)
        {
            os << ",";
        }
        first = false;
        os << "\"" << EscapeJson(item.first) << "\":" << item.second;
    }
    os << "}";
    return os.str();
}

void Increment(std::map<std::string, int>& values, const std::string& key)
{
    ++values[key];
}

void WriteTopoTrackSummary(
    const CaseRecipe& recipe,
    const sggk::ModelingRetPtr& ret,
    const InputTopologyIndex& inputIndex,
    const fs::path& caseDir)
{
    std::map<std::string, int> trackTypeCounts;
    std::map<std::string, int> descendentTypeCounts;
    std::map<std::string, int> ancestorTypeCounts;
    std::map<std::string, int> ancestorInputRoleCounts;
    std::map<std::string, int> ancestorTrackRoleCounts;
    int itemCount = 0;
    int ancestorCount = 0;
    int unresolvedAncestorCount = 0;
    int ambiguousAncestorCount = 0;
    int nonTopologyAncestorCount = 0;

    for (const auto& itemPtr : ret->QueryTopoTrackItems())
    {
        if (!itemPtr)
        {
            continue;
        }
        ++itemCount;
        Increment(trackTypeCounts, TrackTypeName(itemPtr->trackType));
        if (itemPtr->descendent)
        {
            Increment(descendentTypeCounts, TopoTypeName(itemPtr->descendent->TopoType()));
        }
        for (const auto& ancestor : itemPtr->ancestors)
        {
            const auto entity = std::dynamic_pointer_cast<sggk::Entity>(ancestor);
            if (!entity)
            {
                continue;
            }
            ++ancestorCount;
            const auto topo = sggk::Entity::Cast<sggk::Topology>(entity);
            Increment(ancestorTypeCounts, topo ? TopoTypeName(topo->TopoType()) : "Entity");
            if (!topo)
            {
                ++nonTopologyAncestorCount;
                continue;
            }

            bool ambiguous = false;
            const auto* inputRef = FindInputTopologyRef(inputIndex, topo, ambiguous);
            if (inputRef)
            {
                Increment(ancestorInputRoleCounts, inputRef->role);
                Increment(ancestorTrackRoleCounts, TrackTypeName(itemPtr->trackType) + "|" + inputRef->role);
            }
            else if (ambiguous)
            {
                ++ambiguousAncestorCount;
            }
            else
            {
                ++unresolvedAncestorCount;
            }
        }
    }

    std::ostringstream os;
    os << "{\n"
       << "  \"dsl\": " << DslProvenanceJson(recipe, 4) << ",\n"
       << "  \"input_operations\": {\n"
       << "    \"target\": " << StringArrayJson(recipe.boolean.target.operations) << ",\n"
       << "    \"tool\": " << StringArrayJson(recipe.boolean.tool.operations) << "\n"
       << "  },\n"
       << "  \"item_count\": " << itemCount << ",\n"
       << "  \"ancestor_count\": " << ancestorCount << ",\n"
       << "  \"resolved_ancestor_count\": " << (ancestorCount - unresolvedAncestorCount - ambiguousAncestorCount - nonTopologyAncestorCount) << ",\n"
       << "  \"unresolved_ancestor_count\": " << unresolvedAncestorCount << ",\n"
       << "  \"ambiguous_ancestor_count\": " << ambiguousAncestorCount << ",\n"
       << "  \"non_topology_ancestor_count\": " << nonTopologyAncestorCount << ",\n"
       << "  \"track_type_counts\": " << StringIntMapJson(trackTypeCounts) << ",\n"
       << "  \"descendent_type_counts\": " << StringIntMapJson(descendentTypeCounts) << ",\n"
       << "  \"ancestor_type_counts\": " << StringIntMapJson(ancestorTypeCounts) << ",\n"
       << "  \"ancestor_input_role_counts\": " << StringIntMapJson(ancestorInputRoleCounts) << ",\n"
       << "  \"ancestor_track_role_counts\": " << StringIntMapJson(ancestorTrackRoleCounts) << "\n"
       << "}\n";
    WriteTextFile(caseDir / "report" / "topo_track_summary.json", os.str());
}

void WriteEmptyTopoTrack(const fs::path& caseDir, const std::string& reason)
{
    std::ostringstream os;
    os << "{\n"
       << "  \"items\": [],\n"
       << "  \"note\": \"" << EscapeJson(reason) << "\"\n"
       << "}\n";
    WriteTextFile(caseDir / "report" / "topo_track.json", os.str());
}

void WriteSkippedTopoTrackSummary(const CaseRecipe& recipe, const fs::path& caseDir, const std::string& reason)
{
    std::ostringstream os;
    os << "{\n"
       << "  \"dsl\": " << DslProvenanceJson(recipe, 4) << ",\n"
       << "  \"input_operations\": {\n"
       << "    \"target\": " << StringArrayJson(recipe.boolean.target.operations) << ",\n"
       << "    \"tool\": " << StringArrayJson(recipe.boolean.tool.operations) << "\n"
       << "  },\n"
       << "  \"skipped\": true,\n"
       << "  \"reason\": \"" << EscapeJson(reason) << "\",\n"
       << "  \"item_count\": 0,\n"
       << "  \"ancestor_count\": 0,\n"
       << "  \"resolved_ancestor_count\": 0,\n"
       << "  \"unresolved_ancestor_count\": 0,\n"
       << "  \"ambiguous_ancestor_count\": 0,\n"
       << "  \"non_topology_ancestor_count\": 0,\n"
       << "  \"track_type_counts\": {},\n"
       << "  \"descendent_type_counts\": {},\n"
       << "  \"ancestor_type_counts\": {},\n"
       << "  \"ancestor_input_role_counts\": {},\n"
       << "  \"ancestor_track_role_counts\": {}\n"
       << "}\n";
    WriteTextFile(caseDir / "report" / "topo_track_summary.json", os.str());
}

void CopySourceFileIfPresent(const fs::path& sourceFile, const fs::path& caseDir)
{
    if (sourceFile.empty() || !fs::exists(sourceFile))
    {
        return;
    }
    fs::create_directories(caseDir / "input");
    fs::copy_file(
        sourceFile,
        caseDir / "input" / ("source" + sourceFile.extension().string()),
        fs::copy_options::overwrite_existing);
}

void SerializeResultBodies(const std::vector<sggk::BodyPtr>& bodies, const fs::path& caseDir)
{
    int index = 0;
    for (const auto& body : bodies)
    {
        SerializeTopology(body, caseDir / "output" / ("result_" + std::to_string(++index) + ".sgt"));
    }
}

std::vector<sggk::BodyPtr> ToBodyVector(const sggk::BodyList& bodies)
{
    std::vector<sggk::BodyPtr> result;
    for (const auto& body : bodies)
    {
        result.push_back(body);
    }
    return result;
}

bool DataExchangeStrictSucceeded(const sggk::DataExchangeRet& ret)
{
    return ret.Succeeded() && ret.FailedItems().empty() && ret.InvalidTopos().empty();
}

unsigned int DataExchangeStrictErrorCode(const sggk::DataExchangeRet& ret)
{
    if (DataExchangeStrictSucceeded(ret))
    {
        return 0;
    }
    const unsigned int code = ret.Status().ErrorCode();
    return code != 0 ? code : 1;
}

std::string DataExchangeStrictMessage(const sggk::DataExchangeRet& ret, const std::string& phase)
{
    if (DataExchangeStrictSucceeded(ret))
    {
        return ret.Status().ErrorMsg();
    }
    if (!ret.Status().ErrorMsg().empty())
    {
        return ret.Status().ErrorMsg();
    }
    std::ostringstream os;
    os << phase << " returned partial data exchange diagnostics"
       << " failed_items=" << ret.FailedItems().size()
       << " invalid_topologies=" << ret.InvalidTopos().size();
    return os.str();
}

std::string FailedItemsJson(const sggk::FailedItemList& items, size_t limit = 16)
{
    std::ostringstream os;
    os << "[";
    size_t index = 0;
    for (const auto& item : items)
    {
        if (index >= limit)
        {
            break;
        }
        if (index != 0)
        {
            os << ",";
        }
        os << "{"
           << "\"topology_id\":" << item.topoID
           << ",\"error_code\":" << item.errorCode
           << ",\"error_message\":\"" << EscapeJson(item.errorMsg) << "\""
           << "}";
        ++index;
    }
    os << "]";
    return os.str();
}

std::string InvalidToposJson(const sggk::TopoErrorItemList& items, size_t limit = 16)
{
    std::ostringstream os;
    os << "[";
    size_t index = 0;
    for (const auto& item : items)
    {
        if (index >= limit)
        {
            break;
        }
        if (index != 0)
        {
            os << ",";
        }
        os << "{"
           << "\"topology\":" << TopologyBriefJson(item.errorTopo)
           << ",\"error_code\":" << static_cast<unsigned int>(item.errorInfo.errorCode)
           << ",\"error_string\":\"" << EscapeJson(item.errorInfo.ErrorString()) << "\""
           << "}";
        ++index;
    }
    os << "]";
    return os.str();
}

std::string DataExchangeSummaryJson(const sggk::DataExchangeRet& ret, const std::string& phase)
{
    std::ostringstream os;
    os << "{"
       << "\"phase\":\"" << EscapeJson(phase) << "\""
       << ",\"succeeded\":" << (ret.Succeeded() ? "true" : "false")
       << ",\"strict_succeeded\":" << (DataExchangeStrictSucceeded(ret) ? "true" : "false")
       << ",\"error_code\":" << ret.Status().ErrorCode()
       << ",\"error_message\":\"" << EscapeJson(ret.Status().ErrorMsg()) << "\""
       << ",\"failed_item_count\":" << ret.FailedItems().size()
       << ",\"invalid_topology_count\":" << ret.InvalidTopos().size()
       << ",\"failed_items\":" << FailedItemsJson(ret.FailedItems())
       << ",\"invalid_topologies\":" << InvalidToposJson(ret.InvalidTopos())
       << "}";
    return os.str();
}

void WriteDataExchangeReport(
    size_t failedItemCount,
    size_t invalidTopoCount,
    double lengthScale,
    const fs::path& caseDir)
{
    std::ostringstream os;
    os << "{\n"
       << "  \"failed_item_count\": " << failedItemCount << ",\n"
       << "  \"invalid_topology_count\": " << invalidTopoCount << ",\n"
       << "  \"length_scale\": " << std::setprecision(17) << lengthScale << "\n"
       << "}\n";
    WriteTextFile(caseDir / "report" / "data_exchange.json", os.str());
}

void WriteDataExchangeRoundtripReport(
    const std::string& format,
    const fs::path& exchangeFile,
    int sourceBodyIndex,
    const sggk::DataExchangeRet& exportRet,
    const sggk::DataExchangeRet* importRet,
    double importLengthScale,
    size_t importedBodyCount,
    const fs::path& caseDir)
{
    const size_t importFailedCount = importRet ? importRet->FailedItems().size() : 0;
    const size_t importInvalidCount = importRet ? importRet->InvalidTopos().size() : 0;
    std::ostringstream os;
    os << "{\n"
       << "  \"format\": \"" << EscapeJson(format) << "\",\n"
       << "  \"exchange_file\": \"" << EscapeJson(exchangeFile.string()) << "\",\n"
       << "  \"source_body_index\": " << sourceBodyIndex << ",\n"
       << "  \"failed_item_count\": " << (exportRet.FailedItems().size() + importFailedCount) << ",\n"
       << "  \"invalid_topology_count\": " << (exportRet.InvalidTopos().size() + importInvalidCount) << ",\n"
       << "  \"import_length_scale\": " << std::setprecision(17) << importLengthScale << ",\n"
       << "  \"imported_body_count\": " << importedBodyCount << ",\n"
       << "  \"export\": " << DataExchangeSummaryJson(exportRet, "export") << ",\n"
       << "  \"import\": " << (importRet ? DataExchangeSummaryJson(*importRet, "import") : "null") << "\n"
       << "}\n";
    WriteTextFile(caseDir / "report" / "data_exchange.json", os.str());
}

void WriteSourceProperties(const std::vector<BodyProperties>& properties, const fs::path& caseDir)
{
    std::ostringstream os;
    os << "{\n  \"source\": [\n";
    for (size_t i = 0; i < properties.size(); ++i)
    {
        if (i != 0)
        {
            os << ",\n";
        }
        os << "    " << BodyPropertyJson(properties[i]);
    }
    os << "\n  ]\n}\n";
    WriteTextFile(caseDir / "report" / "source_properties.json", os.str());
}

bool RoundtripClose(double actual, double expected, double absTol, double relTol)
{
    return std::fabs(actual - expected) <= CompareTolerance(absTol, relTol, actual, expected);
}

std::string RoundtripMetricJson(
    const std::string& name,
    double source,
    double result,
    double absTol,
    double relTol,
    std::vector<std::string>& failures)
{
    const double delta = result - source;
    const double tolerance = CompareTolerance(absTol, relTol, result, source);
    const bool ok = std::fabs(delta) <= tolerance;
    if (!ok)
    {
        failures.push_back("roundtrip_" + name + "_mismatch");
    }
    std::ostringstream os;
    os << "{"
       << "\"source\":" << std::setprecision(17) << source
       << ",\"result\":" << std::setprecision(17) << result
       << ",\"delta\":" << std::setprecision(17) << delta
       << ",\"tolerance\":" << std::setprecision(17) << tolerance
       << ",\"ok\":" << (ok ? "true" : "false")
       << "}";
    return os.str();
}

std::string BBoxAggregateJson(const BBoxAggregate& box)
{
    if (!box.ok)
    {
        return "null";
    }
    std::ostringstream os;
    os << "{"
       << "\"min\":[" << std::setprecision(17) << box.minX
       << "," << std::setprecision(17) << box.minY
       << "," << std::setprecision(17) << box.minZ << "]"
       << ",\"max\":[" << std::setprecision(17) << box.maxX
       << "," << std::setprecision(17) << box.maxY
       << "," << std::setprecision(17) << box.maxZ << "]"
       << "}";
    return os.str();
}

std::string RoundtripBBoxComparisonJson(
    const BBoxAggregate& source,
    const BBoxAggregate& result,
    double absTol,
    double relTol,
    std::vector<std::string>& failures)
{
    bool ok = source.ok && result.ok;
    if (ok)
    {
        ok = RoundtripClose(result.minX, source.minX, absTol, relTol) &&
             RoundtripClose(result.minY, source.minY, absTol, relTol) &&
             RoundtripClose(result.minZ, source.minZ, absTol, relTol) &&
             RoundtripClose(result.maxX, source.maxX, absTol, relTol) &&
             RoundtripClose(result.maxY, source.maxY, absTol, relTol) &&
             RoundtripClose(result.maxZ, source.maxZ, absTol, relTol);
    }
    if (!ok)
    {
        failures.push_back("roundtrip_bbox_mismatch");
    }
    std::ostringstream os;
    os << "{"
       << "\"source\":" << BBoxAggregateJson(source)
       << ",\"result\":" << BBoxAggregateJson(result)
       << ",\"ok\":" << (ok ? "true" : "false")
       << "}";
    return os.str();
}

bool WriteRoundtripComparison(
    const CaseRecipe& recipe,
    const std::vector<BodyProperties>& sourceProperties,
    const std::vector<BodyProperties>& resultProperties,
    const fs::path& caseDir)
{
    std::vector<std::string> failures;
    if (!AllPropertiesOk(sourceProperties))
    {
        failures.push_back("roundtrip_source_properties_unavailable");
    }
    if (!AllPropertiesOk(resultProperties))
    {
        failures.push_back("roundtrip_result_properties_unavailable");
    }

    const double sourceLength = TotalMetric(sourceProperties, &BodyProperties::length);
    const double resultLength = TotalMetric(resultProperties, &BodyProperties::length);
    const double sourceArea = TotalMetric(sourceProperties, &BodyProperties::area);
    const double resultArea = TotalMetric(resultProperties, &BodyProperties::area);
    const double sourceAbsVolume = TotalAbsVolume(sourceProperties);
    const double resultAbsVolume = TotalAbsVolume(resultProperties);
    const auto sourceBox = AggregateBBox(sourceProperties);
    const auto resultBox = AggregateBBox(resultProperties);

    const std::string lengthJson = RoundtripMetricJson(
        "length",
        sourceLength,
        resultLength,
        recipe.roundtripAbsTol,
        recipe.roundtripRelTol,
        failures);
    const std::string areaJson = RoundtripMetricJson(
        "area",
        sourceArea,
        resultArea,
        recipe.roundtripAbsTol,
        recipe.roundtripRelTol,
        failures);
    const std::string absVolumeJson = RoundtripMetricJson(
        "abs_volume",
        sourceAbsVolume,
        resultAbsVolume,
        recipe.roundtripAbsTol,
        recipe.roundtripRelTol,
        failures);
    const std::string bboxJson = RoundtripBBoxComparisonJson(
        sourceBox,
        resultBox,
        recipe.roundtripAbsTol,
        recipe.roundtripRelTol,
        failures);

    const bool ok = failures.empty();
    std::ostringstream os;
    os << "{\n"
       << "  \"ok\": " << (ok ? "true" : "false") << ",\n"
       << "  \"abs_tol\": " << std::setprecision(17) << recipe.roundtripAbsTol << ",\n"
       << "  \"rel_tol\": " << std::setprecision(17) << recipe.roundtripRelTol << ",\n"
       << "  \"metrics\": {\n"
       << "    \"length\": " << lengthJson << ",\n"
       << "    \"area\": " << areaJson << ",\n"
       << "    \"abs_volume\": " << absVolumeJson << "\n"
       << "  },\n"
       << "  \"bbox\": " << bboxJson << ",\n"
       << "  \"failures\": " << StringArrayJson(failures) << "\n"
       << "}\n";
    WriteTextFile(caseDir / "report" / "roundtrip_comparison.json", os.str());
    return ok;
}

int FinishCapturedBodies(
    const CaseRecipe& recipe,
    const std::vector<sggk::BodyPtr>& resultBodies,
    bool apiSucceeded,
    const fs::path& caseDir)
{
    SerializeResultBodies(resultBodies, caseDir);
    const bool topoOk = WriteTopoCheck(resultBodies, caseDir);
    const auto resultProperties = ComputeBodyProperties(resultBodies);
    WriteProperties(resultProperties, caseDir);
    const bool validationOk = WriteValidation(recipe, resultBodies, resultProperties, {}, {}, {}, {}, caseDir);
    WriteEmptyTopoTrack(caseDir, recipe.api + " does not currently expose ModelingRet topology tracking");

    std::cout << "case_id=" << recipe.caseId << "\n"
              << "succeeded=" << (apiSucceeded ? "true" : "false") << "\n"
              << "topology_ok=" << (topoOk ? "true" : "false") << "\n"
              << "validation_ok=" << (validationOk ? "true" : "false") << "\n"
              << "artifact_dir=" << fs::absolute(caseDir).string() << "\n";
    return (apiSucceeded && topoOk && validationOk) ? 0 : 2;
}

int FinishCapturedTopologies(
    const CaseRecipe& recipe,
    const std::vector<sggk::TopologyPtr>& resultTopologies,
    bool apiSucceeded,
    const fs::path& caseDir)
{
    int index = 0;
    std::map<std::string, int> typeCounts;
    for (const auto& topology : resultTopologies)
    {
        if (!topology)
        {
            continue;
        }
        ++typeCounts[TopoTypeName(topology->TopoType())];
        SerializeTopology(topology, caseDir / "output" / ("topology_" + std::to_string(++index) + ".sgt"));
    }
    const bool topoOk = WriteTopoCheckTopologies(resultTopologies, caseDir);
    WriteProperties({}, caseDir);
    WriteEmptyTopoTrack(caseDir, recipe.api + " loaded a non-body SGT topology asset");

    std::ostringstream validation;
    validation << "{\n"
               << "  \"ok\": " << ((apiSucceeded && topoOk && !resultTopologies.empty()) ? "true" : "false") << ",\n"
               << "  \"expectations\": " << ValidationExpectationsJson(recipe.expectations) << ",\n"
               << "  \"result_body_count\": 0,\n"
               << "  \"result_topology_count\": " << resultTopologies.size() << ",\n"
               << "  \"topology_type_counts\": {";
    bool firstType = true;
    for (const auto& item : typeCounts)
    {
        if (!firstType)
        {
            validation << ",";
        }
        firstType = false;
        validation << "\"" << EscapeJson(item.first) << "\":" << item.second;
    }
    validation << "},\n"
               << "  \"totals\": {\"length\":0,\"area\":0,\"volume\":0,\"abs_volume\":0},\n"
               << "  \"input_totals\": {\"target_abs_volume\":0,\"tool_abs_volume\":0},\n"
               << "  \"point_relations\": [],\n"
               << "  \"face_point_relations\": [],\n"
               << "  \"clash_checks\": [],\n"
               << "  \"distance_checks\": [],\n"
               << "  \"plane_extreme_checks\": [],\n"
               << "  \"failures\": [";
    if (resultTopologies.empty())
    {
        validation << "\"generic_sgt_topology_count_below_min actual=0 min=1\"";
    }
    validation << "],\n"
               << "  \"skipped_checks\": [\"non_body_sgt_body_property_oracles_skipped\"]\n"
               << "}\n";
    WriteTextFile(caseDir / "report" / "validation.json", validation.str());
    WriteTextFile(
        caseDir / "report" / "debug_geometry_index.json",
        "{\n  \"case_id\": \"" + EscapeJson(recipe.caseId) + "\",\n  \"assets\": []\n}\n");

    std::cout << "case_id=" << recipe.caseId << "\n"
              << "succeeded=" << (apiSucceeded ? "true" : "false") << "\n"
              << "topology_ok=" << (topoOk ? "true" : "false") << "\n"
              << "validation_ok=" << ((apiSucceeded && topoOk && !resultTopologies.empty()) ? "true" : "false") << "\n"
              << "artifact_dir=" << fs::absolute(caseDir).string() << "\n";
    return (apiSucceeded && topoOk && !resultTopologies.empty()) ? 0 : 2;
}

int FinishRoundtripCapturedBodies(
    const CaseRecipe& recipe,
    const std::vector<sggk::BodyPtr>& resultBodies,
    const std::vector<BodyProperties>& sourceProperties,
    bool apiSucceeded,
    const fs::path& caseDir)
{
    SerializeResultBodies(resultBodies, caseDir);
    const bool topoOk = WriteTopoCheck(resultBodies, caseDir);
    const auto resultProperties = ComputeBodyProperties(resultBodies);
    WriteProperties(resultProperties, caseDir);
    const bool validationOk = WriteValidation(recipe, resultBodies, resultProperties, {}, {}, {}, {}, caseDir);
    const bool roundtripOk = WriteRoundtripComparison(recipe, sourceProperties, resultProperties, caseDir);
    WriteEmptyTopoTrack(caseDir, recipe.api + " does not currently expose ModelingRet topology tracking");

    std::cout << "case_id=" << recipe.caseId << "\n"
              << "succeeded=" << (apiSucceeded ? "true" : "false") << "\n"
              << "topology_ok=" << (topoOk ? "true" : "false") << "\n"
              << "validation_ok=" << (validationOk ? "true" : "false") << "\n"
              << "roundtrip_ok=" << (roundtripOk ? "true" : "false") << "\n"
              << "artifact_dir=" << fs::absolute(caseDir).string() << "\n";
    return (apiSucceeded && topoOk && validationOk && roundtripOk) ? 0 : 2;
}

int RunApiOffsetBodyCase(const CliOptions& cli, const CaseRecipe& recipe)
{
    if (recipe.offsetSource.kind.empty())
    {
        throw std::runtime_error("api_offset_body requires source_kind");
    }
    if (std::fabs(recipe.offsetDistance) <= 0.0)
    {
        throw std::runtime_error("api_offset_body requires non-zero offset_distance");
    }

    const fs::path caseDir = CaseDirectory(cli.outRoot, recipe.caseId);
    fs::create_directories(caseDir / "input");
    fs::create_directories(caseDir / "output");
    fs::create_directories(caseDir / "report");
    WriteManifest(recipe, cli, caseDir);
    CopySourceFileIfPresent(recipe.offsetSource.sourceFile, caseDir);

    auto source = MakeBodyFromSpec(recipe.offsetSource, "source");
    SerializeTopology(source, caseDir / "input" / "source.sgt");
    const auto sourceProperties = ComputeBodyProperties(
        std::vector<sggk::BodyPtr>{source},
        recipe.expectations.sampleInputProperties);
    WriteInputProperties(sourceProperties, {}, caseDir);
    WriteSourceProperties(sourceProperties, caseDir);

    sggk::OffsetOpts opts;
    opts.SetModelingTol(recipe.modelingTol);
    opts.SetCheckValid(recipe.checkValid);
    opts.SetToTopoTrack(recipe.topoTrack);
    opts.SetNearTangentAngle(recipe.offsetSource.g1Tol);
    opts.SetAllowPartialSuccess(recipe.offsetSource.allowPartialSuccess);

    auto ret = sggk::api_offset_body(source, recipe.offsetDistance, opts);
    if (!ret)
    {
        throw std::runtime_error("api_offset_body returned null");
    }

    WriteStatus(ret, caseDir);
    CaptureErrorEntities(ret->Status(), caseDir);

    const auto resultBodies = ToBodyVector(ret->ResultBodies());
    SerializeResultBodies(resultBodies, caseDir);
    const bool topoOk = WriteTopoCheck(resultBodies, caseDir);
    const auto resultProperties = ComputeBodyProperties(resultBodies);
    WriteProperties(resultProperties, caseDir);
    const bool validationOk = WriteValidation(
        recipe,
        resultBodies,
        resultProperties,
        std::vector<sggk::BodyPtr>{source},
        sourceProperties,
        {},
        {},
        caseDir);
    const std::string topoTrackReason = recipe.topoTrack
        ? "api_offset_body flat recipe topology tracking is captured as status/topocheck artifacts only"
        : "topo_track disabled by recipe";
    WriteEmptyTopoTrack(caseDir, topoTrackReason);
    WriteSkippedTopoTrackSummary(recipe, caseDir, topoTrackReason);

    std::cout << "case_id=" << recipe.caseId << "\n"
              << "succeeded=" << (ret->Succeeded() ? "true" : "false") << "\n"
              << "topology_ok=" << (topoOk ? "true" : "false") << "\n"
              << "validation_ok=" << (validationOk ? "true" : "false") << "\n"
              << "error_code=" << ret->Status().ErrorCode() << "\n"
              << "artifact_dir=" << fs::absolute(caseDir).string() << "\n";
    return (ret->Succeeded() && topoOk && validationOk) ? 0 : 2;
}

int RunSgtCase(const CliOptions& cli, const CaseRecipe& recipe)
{
    if (recipe.sourceFile.empty())
    {
        throw std::runtime_error("check_sgt requires source_file");
    }

    const fs::path caseDir = CaseDirectory(cli.outRoot, recipe.caseId);
    fs::create_directories(caseDir / "input");
    fs::create_directories(caseDir / "output");
    fs::create_directories(caseDir / "report");
    WriteManifest(recipe, cli, caseDir);
    CopySourceFileIfPresent(recipe.sourceFile, caseDir);

    std::vector<sggk::BodyPtr> resultBodies;
    std::vector<sggk::TopologyPtr> resultTopologies;
    std::string errorMessage;
    bool succeeded = false;
    bool loadedGenericTopology = false;
    try
    {
        sggk::RapidTopoJsonDeserializer deserializer;
        auto bodies = deserializer.DeserializeBodiesFromFile(recipe.sourceFile.string().c_str());
        for (const auto& body : bodies)
        {
            resultBodies.push_back(body);
        }
        if (resultBodies.empty())
        {
            auto body = deserializer.DeserializeBodyFromFile(recipe.sourceFile.string().c_str());
            if (body)
            {
                resultBodies.push_back(body);
            }
        }
        succeeded = !resultBodies.empty();
        if (!succeeded)
        {
            auto topologies = deserializer.DeserializeFromFile(recipe.sourceFile.string().c_str());
            for (const auto& topology : topologies)
            {
                if (topology)
                {
                    resultTopologies.push_back(topology);
                }
            }
            loadedGenericTopology = !resultTopologies.empty();
            succeeded = loadedGenericTopology;
        }
        if (!succeeded)
        {
            errorMessage = "SGT deserialized successfully but produced no bodies or topologies";
        }
    }
    catch (const std::exception& ex)
    {
        errorMessage = ex.what();
    }
    catch (...)
    {
        errorMessage = "unknown SGT deserialization failure";
    }

    if (resultBodies.empty() && resultTopologies.empty())
    {
        try
        {
            sggk::RapidTopoJsonDeserializer deserializer;
            auto topologies = deserializer.DeserializeFromFile(recipe.sourceFile.string().c_str());
            for (const auto& topology : topologies)
            {
                if (topology)
                {
                    resultTopologies.push_back(topology);
                }
            }
            loadedGenericTopology = !resultTopologies.empty();
            if (loadedGenericTopology)
            {
                succeeded = true;
                errorMessage.clear();
            }
        }
        catch (const std::exception& ex)
        {
            if (errorMessage.empty())
            {
                errorMessage = ex.what();
            }
        }
        catch (...)
        {
            if (errorMessage.empty())
            {
                errorMessage = "unknown SGT topology deserialization failure";
            }
        }
    }

    WriteStatusGeneric(
        succeeded,
        succeeded ? 0u : 1u,
        loadedGenericTopology && errorMessage.empty() ? "loaded non-body topology asset" : errorMessage,
        0,
        resultBodies.size(),
        resultBodies.size() + resultTopologies.size(),
        caseDir);
    if (loadedGenericTopology)
    {
        return FinishCapturedTopologies(recipe, resultTopologies, succeeded, caseDir);
    }
    return FinishCapturedBodies(recipe, resultBodies, succeeded, caseDir);
}

int RunStepImportCase(const CliOptions& cli, const CaseRecipe& recipe)
{
    if (recipe.sourceFile.empty())
    {
        throw std::runtime_error("step_import requires source_file");
    }

    const fs::path caseDir = CaseDirectory(cli.outRoot, recipe.caseId);
    fs::create_directories(caseDir / "input");
    fs::create_directories(caseDir / "output");
    fs::create_directories(caseDir / "report");
    WriteManifest(recipe, cli, caseDir);
    CopySourceFileIfPresent(recipe.sourceFile, caseDir);

    auto ret = sggk::api_step_import(recipe.sourceFile.string().c_str(), sggk::StepImportOpts());
    if (!ret)
    {
        throw std::runtime_error("api_step_import returned null");
    }

    const auto resultBodies = ToBodyVector(ret->ResultBodies());
    const bool apiOk = DataExchangeStrictSucceeded(*ret);
    WriteStatusGeneric(
        apiOk,
        DataExchangeStrictErrorCode(*ret),
        DataExchangeStrictMessage(*ret, "step_import"),
        ret->FailedItems().size() + ret->InvalidTopos().size(),
        resultBodies.size(),
        caseDir);
    WriteDataExchangeReport(ret->FailedItems().size(), ret->InvalidTopos().size(), ret->LengthScale(), caseDir);
    return FinishCapturedBodies(recipe, resultBodies, apiOk, caseDir);
}

int RunIgesImportCase(const CliOptions& cli, const CaseRecipe& recipe)
{
    if (recipe.sourceFile.empty())
    {
        throw std::runtime_error("iges_import requires source_file");
    }

    const fs::path caseDir = CaseDirectory(cli.outRoot, recipe.caseId);
    fs::create_directories(caseDir / "input");
    fs::create_directories(caseDir / "output");
    fs::create_directories(caseDir / "report");
    WriteManifest(recipe, cli, caseDir);
    CopySourceFileIfPresent(recipe.sourceFile, caseDir);

    auto ret = sggk::api_iges_import(recipe.sourceFile.string().c_str(), sggk::IgesImportOpts());
    if (!ret)
    {
        throw std::runtime_error("api_iges_import returned null");
    }

    const auto resultBodies = ToBodyVector(ret->ResultBodies());
    const bool apiOk = DataExchangeStrictSucceeded(*ret);
    WriteStatusGeneric(
        apiOk,
        DataExchangeStrictErrorCode(*ret),
        DataExchangeStrictMessage(*ret, "iges_import"),
        ret->FailedItems().size() + ret->InvalidTopos().size(),
        resultBodies.size(),
        caseDir);
    WriteDataExchangeReport(ret->FailedItems().size(), ret->InvalidTopos().size(), ret->LengthScale(), caseDir);
    return FinishCapturedBodies(recipe, resultBodies, apiOk, caseDir);
}

int RunStepRoundtripCase(const CliOptions& cli, const CaseRecipe& recipe)
{
    if (recipe.sourceFile.empty())
    {
        throw std::runtime_error("step_roundtrip requires source_file");
    }
    if (recipe.sourceBodyIndex < 0)
    {
        throw std::runtime_error("step_roundtrip source_body_index must be >= 0");
    }

    const fs::path caseDir = CaseDirectory(cli.outRoot, recipe.caseId);
    fs::create_directories(caseDir / "input");
    fs::create_directories(caseDir / "output");
    fs::create_directories(caseDir / "report");
    WriteManifest(recipe, cli, caseDir);
    CopySourceFileIfPresent(recipe.sourceFile, caseDir);

    const auto sourceBody = SelectSgtBody(recipe.sourceFile, recipe.sourceBodyIndex, "step_roundtrip source");
    SerializeTopology(sourceBody, caseDir / "input" / "source_body.sgt");
    const auto sourceProperties = ComputeBodyProperties(std::vector<sggk::BodyPtr>{sourceBody});
    WriteSourceProperties(sourceProperties, caseDir);

    sggk::StepExportOpts exportOpts;
    exportOpts.appProtocol = ParseStepAppProtocol(recipe.stepAppProtocol);
    exportOpts.surfaceToBSpline = recipe.stepSurfaceToBSpline;
    exportOpts.curveToBSpline = recipe.stepCurveToBSpline;
    exportOpts.spcurveInWireToBSpline = recipe.stepSpcurveInWireToBSpline;

    const fs::path exchangeFile = caseDir / "output" / "roundtrip.step";
    auto exportRet = sggk::api_step_export(sourceBody, exchangeFile.string().c_str(), exportOpts);
    if (!exportRet)
    {
        throw std::runtime_error("api_step_export returned null");
    }

    sggk::StepImportRetPtr importRet;
    std::vector<sggk::BodyPtr> resultBodies;
    double lengthScale = 0.0;
    const bool exportOk = DataExchangeStrictSucceeded(*exportRet);
    if (exportOk && fs::exists(exchangeFile))
    {
        importRet = sggk::api_step_import(exchangeFile.string().c_str(), sggk::StepImportOpts());
        if (!importRet)
        {
            throw std::runtime_error("api_step_import returned null during step_roundtrip");
        }
        resultBodies = ToBodyVector(importRet->ResultBodies());
        lengthScale = importRet->LengthScale();
    }

    const bool importOk = importRet && DataExchangeStrictSucceeded(*importRet);
    unsigned int statusCode = 0;
    std::string statusMessage;
    if (!exportOk)
    {
        statusCode = DataExchangeStrictErrorCode(*exportRet);
        statusMessage = DataExchangeStrictMessage(*exportRet, "step_roundtrip export");
    }
    else if (!importRet)
    {
        statusCode = 1;
        statusMessage = "step_roundtrip import skipped because the exchange file was not written";
    }
    else if (!importOk)
    {
        statusCode = DataExchangeStrictErrorCode(*importRet);
        statusMessage = DataExchangeStrictMessage(*importRet, "step_roundtrip import");
    }

    const bool apiOk = exportOk && importOk;
    const size_t exportDiagnostics = exportRet->FailedItems().size() + exportRet->InvalidTopos().size();
    const size_t importDiagnostics = importRet ? importRet->FailedItems().size() + importRet->InvalidTopos().size() : 0;
    WriteStatusGeneric(apiOk, statusCode, statusMessage, exportDiagnostics + importDiagnostics, resultBodies.size(), caseDir);
    WriteDataExchangeRoundtripReport(
        "STEP",
        exchangeFile,
        recipe.sourceBodyIndex,
        *exportRet,
        importRet ? importRet.get() : nullptr,
        lengthScale,
        resultBodies.size(),
        caseDir);
    return FinishRoundtripCapturedBodies(recipe, resultBodies, sourceProperties, apiOk, caseDir);
}

int RunIgesRoundtripCase(const CliOptions& cli, const CaseRecipe& recipe)
{
    if (recipe.sourceFile.empty())
    {
        throw std::runtime_error("iges_roundtrip requires source_file");
    }
    if (recipe.sourceBodyIndex < 0)
    {
        throw std::runtime_error("iges_roundtrip source_body_index must be >= 0");
    }

    const fs::path caseDir = CaseDirectory(cli.outRoot, recipe.caseId);
    fs::create_directories(caseDir / "input");
    fs::create_directories(caseDir / "output");
    fs::create_directories(caseDir / "report");
    WriteManifest(recipe, cli, caseDir);
    CopySourceFileIfPresent(recipe.sourceFile, caseDir);

    const auto sourceBody = SelectSgtBody(recipe.sourceFile, recipe.sourceBodyIndex, "iges_roundtrip source");
    SerializeTopology(sourceBody, caseDir / "input" / "source_body.sgt");
    const auto sourceProperties = ComputeBodyProperties(std::vector<sggk::BodyPtr>{sourceBody});
    WriteSourceProperties(sourceProperties, caseDir);

    sggk::IgesExportOpts exportOpts;
    exportOpts.faceOnlyMode = recipe.igesFaceOnlyMode;
    exportOpts.writeSGKSpecifiedData = recipe.igesWriteSGKSpecifiedData;

    const fs::path exchangeFile = caseDir / "output" / "roundtrip.iges";
    auto exportRet = sggk::api_iges_export(sourceBody, exchangeFile.string().c_str(), exportOpts);
    if (!exportRet)
    {
        throw std::runtime_error("api_iges_export returned null");
    }

    sggk::IgesImportRetPtr importRet;
    std::vector<sggk::BodyPtr> resultBodies;
    double lengthScale = 0.0;
    const bool exportOk = DataExchangeStrictSucceeded(*exportRet);
    if (exportOk && fs::exists(exchangeFile))
    {
        importRet = sggk::api_iges_import(exchangeFile.string().c_str(), sggk::IgesImportOpts());
        if (!importRet)
        {
            throw std::runtime_error("api_iges_import returned null during iges_roundtrip");
        }
        resultBodies = ToBodyVector(importRet->ResultBodies());
        lengthScale = importRet->LengthScale();
    }

    const bool importOk = importRet && DataExchangeStrictSucceeded(*importRet);
    unsigned int statusCode = 0;
    std::string statusMessage;
    if (!exportOk)
    {
        statusCode = DataExchangeStrictErrorCode(*exportRet);
        statusMessage = DataExchangeStrictMessage(*exportRet, "iges_roundtrip export");
    }
    else if (!importRet)
    {
        statusCode = 1;
        statusMessage = "iges_roundtrip import skipped because the exchange file was not written";
    }
    else if (!importOk)
    {
        statusCode = DataExchangeStrictErrorCode(*importRet);
        statusMessage = DataExchangeStrictMessage(*importRet, "iges_roundtrip import");
    }

    const bool apiOk = exportOk && importOk;
    const size_t exportDiagnostics = exportRet->FailedItems().size() + exportRet->InvalidTopos().size();
    const size_t importDiagnostics = importRet ? importRet->FailedItems().size() + importRet->InvalidTopos().size() : 0;
    WriteStatusGeneric(apiOk, statusCode, statusMessage, exportDiagnostics + importDiagnostics, resultBodies.size(), caseDir);
    WriteDataExchangeRoundtripReport(
        "IGES",
        exchangeFile,
        recipe.sourceBodyIndex,
        *exportRet,
        importRet ? importRet.get() : nullptr,
        lengthScale,
        resultBodies.size(),
        caseDir);
    return FinishRoundtripCapturedBodies(recipe, resultBodies, sourceProperties, apiOk, caseDir);
}

fs::path PrepareCaseDirectory(const CliOptions& cli, const CaseRecipe& recipe)
{
    const fs::path caseDir = CaseDirectory(cli.outRoot, recipe.caseId);
    fs::create_directories(caseDir / "input");
    fs::create_directories(caseDir / "output");
    fs::create_directories(caseDir / "report");
    WriteManifest(recipe, cli, caseDir);
    return caseDir;
}

struct BinaryBodyInputs
{
    sggk::BodyPtr target;
    sggk::BodyPtr tool;
    InputTopologyIndex topologyIndex;
    std::vector<BodyProperties> targetProperties;
    std::vector<BodyProperties> toolProperties;
};

BinaryBodyInputs PrepareBinaryBodyInputs(const CaseRecipe& recipe, const fs::path& caseDir)
{
    BinaryBodyInputs inputs;
    inputs.target = MakeBodyFromSpec(recipe.boolean.target, "target");
    inputs.tool = MakeBodyFromSpec(recipe.boolean.tool, "tool");
    SerializeTopology(inputs.target, caseDir / "input" / "target.sgt");
    SerializeTopology(inputs.tool, caseDir / "input" / "tool.sgt");
    WriteInputProvenance(recipe, inputs.target, inputs.tool, caseDir);
    inputs.topologyIndex = BuildInputTopologyIndex(recipe, inputs.target, inputs.tool);
    WriteInputTopologyIndex(recipe, inputs.topologyIndex, caseDir);
    const bool sampleInputProperties = recipe.expectations.sampleInputProperties;
    inputs.targetProperties = ComputeBodyProperties(
        std::vector<sggk::BodyPtr>{inputs.target},
        sampleInputProperties);
    inputs.toolProperties = ComputeBodyProperties(
        std::vector<sggk::BodyPtr>{inputs.tool},
        sampleInputProperties);
    WriteInputProperties(inputs.targetProperties, inputs.toolProperties, caseDir);
    return inputs;
}

template <typename ModelingResultPtr>
void WriteBinaryTopoTracking(
    const CaseRecipe& recipe,
    const ModelingResultPtr& ret,
    const BinaryBodyInputs& inputs,
    const fs::path& caseDir,
    bool captureFlatTopoTrack)
{
    if (recipe.topoTrack && (!recipe.dslSource.empty() || captureFlatTopoTrack))
    {
        WriteTopoTrack(recipe, ret, inputs.topologyIndex, caseDir);
        WriteTopoTrackSummary(recipe, ret, inputs.topologyIndex, caseDir);
    }
    else
    {
        const std::string reason = recipe.topoTrack
            ? "flat recipe TopoTrack capture requires isolated --capture-flat-topotrack execution"
            : "topo_track disabled by recipe";
        WriteEmptyTopoTrack(caseDir, reason);
        WriteSkippedTopoTrackSummary(recipe, caseDir, reason);
    }
}

int RunOffset2DCase(const CliOptions& cli, const CaseRecipe& recipe)
{
    const fs::path caseDir = PrepareCaseDirectory(cli, recipe);
    WriteTextFile(caseDir / "input" / "offset2d_path.json", Offset2DRecipeJson(recipe.offset2d) + "\n");

    auto path = MakeOffset2DPath(recipe.offset2d);
    sggk::Offset2DOpts opts;
    opts.tol = sggk::Toler(recipe.offset2d.distTol, recipe.offset2d.angleTol);
    opts.connectType = ParseOffset2DConnType(recipe.offset2d.connectType);
    opts.allowCrvDegenerated = recipe.offset2d.allowCrvDegenerated;
    opts.allowCrvReversed = recipe.offset2d.allowCrvReversed;
    opts.allowSelfIntersections = recipe.offset2d.allowSelfIntersections;
    opts.extendType = ParseOffset2DExtendType(recipe.offset2d.extendType);

    const sggk::Offset2DResult result = recipe.offset2d.distances.empty()
        ? sggk::Offset2D::Perform(path, recipe.offset2d.distance, opts)
        : sggk::Offset2D::Perform(path, recipe.offset2d.distances, opts);

    const bool apiSucceeded = result.status == sggk::Offset2DStatus::Success;
    WriteOffset2DStatus(recipe, result, caseDir);
    WriteOffset2DResult(result, caseDir);
    const bool validationOk = WriteOffset2DValidation(recipe, result, caseDir);

    std::cout << "case_id=" << recipe.caseId << "\n"
              << "succeeded=" << (apiSucceeded ? "true" : "false") << "\n"
              << "validation_ok=" << (validationOk ? "true" : "false") << "\n"
              << "offset2d_status=" << Offset2DStatusName(result.status) << "\n"
              << "result_path_count=" << result.resultPaths.size() << "\n"
              << "artifact_dir=" << fs::absolute(caseDir).string() << "\n";
    return validationOk ? 0 : 2;
}

int RunBooleanSplitCase(const CliOptions& cli, const CaseRecipe& recipe)
{
    const fs::path caseDir = PrepareCaseDirectory(cli, recipe);
    BinaryBodyInputs inputs = PrepareBinaryBodyInputs(recipe, caseDir);

    sggk::SplitOpts opts;
    opts.SetModelingTol(recipe.modelingTol);
    opts.SetCheckValid(recipe.checkValid);
    opts.SetToTopoTrack(recipe.topoTrack);
    opts.SetNonDestructive(recipe.nonDestructive);
    opts.SetTargetAddFace(recipe.split.targetAddFace);
    opts.SetStrictSplit(recipe.split.strictSplit);
    opts.SetMergeImprint(recipe.split.mergeImprint);

    auto ret = sggk::api_boolean_split(inputs.target, inputs.tool, opts);
    if (!ret)
    {
        throw std::runtime_error("api_boolean_split returned null");
    }

    const auto& outerBodies = ret->ResOuterBodies();
    const auto& innerBodies = ret->ResInnerBodies();
    const auto& wireBodies = ret->ResIntWires();
    const size_t resultCount = outerBodies.size() + innerBodies.size() + wireBodies.size();
    const auto& status = ret->Status();
    WriteStatusGeneric(
        ret->Succeeded(),
        status.ErrorCode(),
        status.ErrorMsg(),
        status.ErrorEntities().size(),
        resultCount,
        caseDir);
    CaptureErrorEntities(status, caseDir);

    int index = 0;
    for (const auto& body : outerBodies)
    {
        SerializeTopology(body, caseDir / "output" / ("outer_" + std::to_string(++index) + ".sgt"));
    }
    index = 0;
    for (const auto& body : innerBodies)
    {
        SerializeTopology(body, caseDir / "output" / ("inner_" + std::to_string(++index) + ".sgt"));
    }
    index = 0;
    for (const auto& body : wireBodies)
    {
        SerializeTopology(body, caseDir / "output" / ("wire_" + std::to_string(++index) + ".sgt"));
    }

    std::vector<sggk::BodyPtr> resultBodies;
    AppendBodies(resultBodies, outerBodies);
    AppendBodies(resultBodies, innerBodies);
    AppendBodies(resultBodies, wireBodies);

    std::vector<std::string> splitFailures;
    const std::string splitJson = SplitResultJson(
        recipe.split,
        outerBodies,
        innerBodies,
        wireBodies,
        splitFailures);
    WriteTextFile(caseDir / "report" / "split_result.json", splitJson + "\n");

    const bool topoOk = WriteTopoCheck(resultBodies, caseDir);
    WriteBinaryTopoTracking(recipe, ret, inputs, caseDir, cli.captureFlatTopoTrack);
    const auto resultProperties = ComputeBodyProperties(resultBodies);
    WriteProperties(resultProperties, caseDir);
    const bool validationOk = WriteValidation(
        recipe,
        resultBodies,
        resultProperties,
        std::vector<sggk::BodyPtr>{inputs.target},
        inputs.targetProperties,
        std::vector<sggk::BodyPtr>{inputs.tool},
        inputs.toolProperties,
        caseDir,
        splitFailures,
        splitJson);

    std::cout << "case_id=" << recipe.caseId << "\n"
              << "succeeded=" << (ret->Succeeded() ? "true" : "false") << "\n"
              << "topology_ok=" << (topoOk ? "true" : "false") << "\n"
              << "validation_ok=" << (validationOk ? "true" : "false") << "\n"
              << "outer_body_count=" << outerBodies.size() << "\n"
              << "inner_body_count=" << innerBodies.size() << "\n"
              << "wire_body_count=" << wireBodies.size() << "\n"
              << "error_code=" << status.ErrorCode() << "\n"
              << "artifact_dir=" << fs::absolute(caseDir).string() << "\n";
    return (ret->Succeeded() && topoOk && validationOk) ? 0 : 2;
}

int RunBooleanSliceCase(const CliOptions& cli, const CaseRecipe& recipe)
{
    const fs::path caseDir = PrepareCaseDirectory(cli, recipe);
    BinaryBodyInputs inputs = PrepareBinaryBodyInputs(recipe, caseDir);

    sggk::BooleanOpts opts(ParseBooleanType(recipe.booleanType));
    opts.SetModelingTol(recipe.modelingTol);
    opts.SetCheckValid(recipe.checkValid);
    opts.SetToTopoTrack(recipe.topoTrack);
    opts.SetNonDestructive(recipe.nonDestructive);
    opts.SetVertexTrack(true);

    auto ret = sggk::api_boolean_slice(inputs.target, inputs.tool, opts);
    if (!ret)
    {
        throw std::runtime_error("api_boolean_slice returned null");
    }

    WriteStatus(ret, caseDir);
    CaptureErrorEntities(ret->Status(), caseDir);

    std::vector<sggk::BodyPtr> resultBodies;
    int index = 0;
    for (const auto& body : ret->ResultBodies())
    {
        if (!body)
        {
            continue;
        }
        resultBodies.push_back(body);
        SerializeTopology(body, caseDir / "output" / ("slice_" + std::to_string(++index) + ".sgt"));
    }

    std::vector<std::string> sliceFailures;
    const std::string sliceJson = SliceResultJson(recipe.slice, resultBodies, sliceFailures);
    WriteTextFile(caseDir / "report" / "slice_result.json", sliceJson + "\n");

    const bool topoOk = WriteTopoCheck(resultBodies, caseDir);
    WriteBinaryTopoTracking(recipe, ret, inputs, caseDir, cli.captureFlatTopoTrack);
    const auto resultProperties = ComputeBodyProperties(resultBodies);
    WriteProperties(resultProperties, caseDir);
    const bool validationOk = WriteValidation(
        recipe,
        resultBodies,
        resultProperties,
        std::vector<sggk::BodyPtr>{inputs.target},
        inputs.targetProperties,
        std::vector<sggk::BodyPtr>{inputs.tool},
        inputs.toolProperties,
        caseDir,
        sliceFailures,
        sliceJson);

    std::cout << "case_id=" << recipe.caseId << "\n"
              << "succeeded=" << (ret->Succeeded() ? "true" : "false") << "\n"
              << "topology_ok=" << (topoOk ? "true" : "false") << "\n"
              << "validation_ok=" << (validationOk ? "true" : "false") << "\n"
              << "slice_body_count=" << resultBodies.size() << "\n"
              << "error_code=" << ret->Status().ErrorCode() << "\n"
              << "artifact_dir=" << fs::absolute(caseDir).string() << "\n";
    return (ret->Succeeded() && topoOk && validationOk) ? 0 : 2;
}

int RunTopologySectionCase(const CliOptions& cli, const CaseRecipe& recipe)
{
    const fs::path caseDir = PrepareCaseDirectory(cli, recipe);
    BinaryBodyInputs inputs = PrepareBinaryBodyInputs(recipe, caseDir);

    sggk::BooleanOpts opts;
    opts.SetModelingTol(recipe.modelingTol);
    opts.SetCheckValid(recipe.checkValid);
    opts.SetToTopoTrack(recipe.topoTrack);
    opts.SetNonDestructive(recipe.nonDestructive);

    const sggk::TopologyPtr targetTopology = inputs.target;
    const sggk::TopologyPtr toolTopology = inputs.tool;
    auto ret = sggk::api_topology_section(targetTopology, toolTopology, opts);
    if (!ret)
    {
        throw std::runtime_error("api_topology_section returned null");
    }

    const auto& edges = ret->ResultEdges();
    const auto& vertices = ret->ResultVertices();
    const size_t edgeCount = static_cast<size_t>(std::count_if(edges.begin(), edges.end(), [](const auto& edge) {
        return static_cast<bool>(edge);
    }));
    const size_t vertexCount = static_cast<size_t>(std::count_if(vertices.begin(), vertices.end(), [](const auto& vertex) {
        return static_cast<bool>(vertex);
    }));
    const size_t resultCount = edgeCount + vertexCount;
    const auto& status = ret->Status();
    WriteStatusGeneric(
        ret->Succeeded(),
        status.ErrorCode(),
        status.ErrorMsg(),
        status.ErrorEntities().size(),
        0,
        resultCount,
        caseDir);
    CaptureErrorEntities(status, caseDir);

    std::vector<sggk::TopologyPtr> resultTopologies;
    int index = 0;
    for (const auto& edge : edges)
    {
        if (!edge)
        {
            continue;
        }
        resultTopologies.push_back(edge);
        SerializeTopology(edge, caseDir / "output" / ("section_edge_" + std::to_string(++index) + ".sgt"));
    }
    index = 0;
    for (const auto& vertex : vertices)
    {
        if (!vertex)
        {
            continue;
        }
        resultTopologies.push_back(vertex);
        SerializeTopology(vertex, caseDir / "output" / ("section_vertex_" + std::to_string(++index) + ".sgt"));
    }

    std::vector<std::string> sectionFailures;
    const std::string sectionJson = TopologySectionResultJson(
        recipe.topologySection,
        static_cast<int>(edgeCount),
        static_cast<int>(vertexCount),
        sectionFailures);
    WriteTextFile(caseDir / "report" / "topology_section_result.json", sectionJson + "\n");

    const bool topoOk = WriteTopoCheckTopologies(resultTopologies, caseDir);
    WriteBinaryTopoTracking(recipe, ret, inputs, caseDir, cli.captureFlatTopoTrack);
    const bool validationOk = sectionFailures.empty();
    std::ostringstream validation;
    validation << "{\n"
               << "  \"ok\": " << (validationOk ? "true" : "false") << ",\n"
               << "  \"oracle_kind\": \"topology_section_counts\",\n"
               << "  \"topology_section\": " << sectionJson << ",\n"
               << "  \"failures\": " << StringArrayJson(sectionFailures) << "\n"
               << "}\n";
    WriteTextFile(caseDir / "report" / "validation.json", validation.str());

    std::cout << "case_id=" << recipe.caseId << "\n"
              << "succeeded=" << (ret->Succeeded() ? "true" : "false") << "\n"
              << "topology_ok=" << (topoOk ? "true" : "false") << "\n"
              << "validation_ok=" << (validationOk ? "true" : "false") << "\n"
              << "section_edge_count=" << edgeCount << "\n"
              << "section_vertex_count=" << vertexCount << "\n"
              << "error_code=" << status.ErrorCode() << "\n"
              << "artifact_dir=" << fs::absolute(caseDir).string() << "\n";
    return (ret->Succeeded() && topoOk && validationOk) ? 0 : 2;
}

using CaseAdapter = int (*)(const CliOptions&, const CaseRecipe&);

#include "generated_plugin_adapters.inc"
#include "generated_plugin_metadata.inc"

const std::map<std::string, CaseAdapter>& FlatRecipeAdapters()
{
    static const std::map<std::string, CaseAdapter> adapters = {
        {"check_sgt", &RunSgtCase},
        {"step_import", &RunStepImportCase},
        {"iges_import", &RunIgesImportCase},
        {"step_roundtrip", &RunStepRoundtripCase},
        {"iges_roundtrip", &RunIgesRoundtripCase},
        {"api_offset2d", &RunOffset2DCase},
        {"api_boolean_split", &RunBooleanSplitCase},
        {"api_boolean_slice", &RunBooleanSliceCase},
        {"api_offset_body", &RunApiOffsetBodyCase},
        {"api_topology_section", &RunTopologySectionCase},
#include "generated_plugin_entries.inc"
    };
    return adapters;
}

std::string AdapterCatalogJson()
{
    const auto& pluginHashes = GeneratedPluginManifestHashes();
    const auto& pluginVersions = GeneratedPluginVersions();
    const auto& pluginContractVersions = GeneratedPluginContractVersions();
    std::ostringstream os;
    os << "{\n  \"schema_version\": 1,\n  \"adapters\": [\n";
    os << "    {\"api\":\"api_boolean\",\"source\":\"builtin\",\"contract_version\":0,\"plugin_version\":0,\"manifest_sha256\":\"\"}";
    bool first = false;
    for (const auto& item : FlatRecipeAdapters())
    {
        if (!first)
        {
            os << ",\n";
        }
        first = false;
        const auto hash = pluginHashes.find(item.first);
        const auto version = pluginVersions.find(item.first);
        const auto contractVersion = pluginContractVersions.find(item.first);
        os << "    {\"api\":\"" << EscapeJson(item.first) << "\""
           << ",\"source\":\"" << (hash == pluginHashes.end() ? "builtin" : "plugin") << "\""
           << ",\"contract_version\":" << (contractVersion == pluginContractVersions.end() ? 0 : contractVersion->second)
           << ",\"plugin_version\":" << (version == pluginVersions.end() ? 0 : version->second)
           << ",\"manifest_sha256\":\"" << (hash == pluginHashes.end() ? "" : hash->second) << "\"}";
    }
    os << "\n  ]\n}\n";
    return os.str();
}

int RunCase(const CliOptions& cli, CaseRecipe recipe)
{
    if (!cli.caseIdOverride.empty())
    {
        recipe.caseId = cli.caseIdOverride;
    }

    const auto adapter = FlatRecipeAdapters().find(recipe.api);
    if (adapter != FlatRecipeAdapters().end())
    {
        return adapter->second(cli, recipe);
    }
    if (recipe.api != "api_boolean")
    {
        throw std::runtime_error("unsupported api: " + recipe.api);
    }

    const fs::path caseDir = CaseDirectory(cli.outRoot, recipe.caseId);
    fs::create_directories(caseDir / "input");
    fs::create_directories(caseDir / "output");
    fs::create_directories(caseDir / "report");
    WriteManifest(recipe, cli, caseDir);

    auto target = MakeBodyFromSpec(recipe.boolean.target, "target");
    auto tool = MakeBodyFromSpec(recipe.boolean.tool, "tool");
    SerializeTopology(target, caseDir / "input" / "target.sgt");
    SerializeTopology(tool, caseDir / "input" / "tool.sgt");
    WriteInputProvenance(recipe, target, tool, caseDir);
    const auto inputTopologyIndex = BuildInputTopologyIndex(recipe, target, tool);
    WriteInputTopologyIndex(recipe, inputTopologyIndex, caseDir);
    const bool sampleInputProperties = recipe.expectations.sampleInputProperties;
    const auto targetProperties = ComputeBodyProperties(std::vector<sggk::BodyPtr>{target}, sampleInputProperties);
    const auto toolProperties = ComputeBodyProperties(std::vector<sggk::BodyPtr>{tool}, sampleInputProperties);
    WriteInputProperties(targetProperties, toolProperties, caseDir);

    sggk::BooleanOpts opts(ParseBooleanType(recipe.booleanType));
    opts.SetModelingTol(recipe.modelingTol);
    opts.SetCheckValid(recipe.checkValid);
    opts.SetToTopoTrack(recipe.topoTrack);
    opts.SetNonDestructive(recipe.nonDestructive);
    opts.SetVertexTrack(true);

    auto ret = sggk::api_boolean(target, tool, opts);
    if (!ret)
    {
        throw std::runtime_error("api_boolean returned null");
    }

    WriteStatus(ret, caseDir);
    CaptureErrorEntities(ret->Status(), caseDir);

    std::vector<sggk::BodyPtr> resultBodies;
    int index = 0;
    for (const auto& body : ret->ResultBodies())
    {
        resultBodies.push_back(body);
        SerializeTopology(body, caseDir / "output" / ("result_" + std::to_string(++index) + ".sgt"));
    }

    const bool topoOk = WriteTopoCheck(resultBodies, caseDir);
    if (recipe.topoTrack && (!recipe.dslSource.empty() || cli.captureFlatTopoTrack))
    {
        WriteTopoTrack(recipe, ret, inputTopologyIndex, caseDir);
        WriteTopoTrackSummary(recipe, ret, inputTopologyIndex, caseDir);
    }
    else
    {
        const std::string reason = recipe.topoTrack
            ? "flat recipe TopoTrack capture requires isolated --capture-flat-topotrack execution"
            : "topo_track disabled by recipe";
        WriteEmptyTopoTrack(caseDir, reason);
        WriteSkippedTopoTrackSummary(recipe, caseDir, reason);
    }
    const auto resultProperties = ComputeBodyProperties(resultBodies);
    WriteProperties(resultProperties, caseDir);
    const bool validationOk = WriteValidation(
        recipe,
        resultBodies,
        resultProperties,
        std::vector<sggk::BodyPtr>{target},
        targetProperties,
        std::vector<sggk::BodyPtr>{tool},
        toolProperties,
        caseDir);

    std::cout << "case_id=" << recipe.caseId << "\n"
              << "succeeded=" << (ret->Succeeded() ? "true" : "false") << "\n"
              << "topology_ok=" << (topoOk ? "true" : "false") << "\n"
              << "validation_ok=" << (validationOk ? "true" : "false") << "\n"
              << "error_code=" << ret->Status().ErrorCode() << "\n"
              << "artifact_dir=" << fs::absolute(caseDir).string() << "\n";
    return (ret->Succeeded() && topoOk && validationOk) ? 0 : 2;
}
}

int main(int argc, char** argv)
{
    try
    {
        const CliOptions cli = ParseCli(argc, argv);
        if (cli.listAdaptersJson)
        {
            std::cout << AdapterCatalogJson();
            return 0;
        }
        std::vector<CaseRecipe> recipes = LoadRecipes(cli.recipePath);
        if (!cli.caseIdOverride.empty() && recipes.size() > 1)
        {
            throw std::runtime_error("--case-id can only override a single-case recipe");
        }
        SggkSession session(cli.sdkThreads);
        int exitCode = 0;
        for (auto& recipe : recipes)
        {
            const int caseExit = RunCase(cli, recipe);
            if (caseExit != 0 && exitCode == 0)
            {
                exitCode = caseExit;
            }
        }
        return exitCode;
    }
    catch (const std::exception& ex)
    {
        std::cerr << "sggk_case_runner: " << ex.what() << "\n";
        return 1;
    }
}
