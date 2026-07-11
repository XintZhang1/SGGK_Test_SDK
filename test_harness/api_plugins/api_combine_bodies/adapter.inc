int RunApiCombineBodiesPlugin(const CliOptions& cli, const CaseRecipe& recipe)
{
    const fs::path caseDir = CaseDirectory(cli.outRoot, recipe.caseId);
    fs::create_directories(caseDir / "input");
    fs::create_directories(caseDir / "output");
    fs::create_directories(caseDir / "report");
    WriteManifest(recipe, cli, caseDir);

    bool clone = true;
    if (!cli.recipePath.empty())
    {
        const std::string json = ReadTextFile(cli.recipePath);
        FindBool(json, "combine_clone", clone);
    }

    auto target = MakeBodyFromSpec(recipe.boolean.target, "target");
    auto tool = MakeBodyFromSpec(recipe.boolean.tool, "tool");
    SerializeTopology(target, caseDir / "input" / "target.sgt");
    SerializeTopology(tool, caseDir / "input" / "tool.sgt");
    WriteInputProvenance(recipe, target, tool, caseDir);

    const auto targetProperties = ComputeBodyProperties(
        std::vector<sggk::BodyPtr>{target},
        recipe.expectations.sampleInputProperties);
    const auto toolProperties = ComputeBodyProperties(
        std::vector<sggk::BodyPtr>{tool},
        recipe.expectations.sampleInputProperties);
    WriteInputProperties(targetProperties, toolProperties, caseDir);

    sggk::BodyList inputs;
    inputs.push_back(target);
    inputs.push_back(tool);
    auto result = sggk::api_combine_bodies(inputs, clone);
    const bool apiSucceeded = static_cast<bool>(result);
    std::vector<sggk::BodyPtr> resultBodies;
    if (result)
    {
        resultBodies.push_back(result);
    }
    WriteStatusGeneric(apiSucceeded, apiSucceeded ? 0U : 1U, apiSucceeded ? "" : "null result", 0, resultBodies.size(), caseDir);
    SerializeResultBodies(resultBodies, caseDir);
    const bool topoOk = apiSucceeded && WriteTopoCheck(resultBodies, caseDir);
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
    const std::string topoTrackReason = "api_combine_bodies returns BodyPtr and does not expose ModelingRet topology tracking";
    WriteEmptyTopoTrack(caseDir, topoTrackReason);
    WriteSkippedTopoTrackSummary(recipe, caseDir, topoTrackReason);

    std::cout << "case_id=" << recipe.caseId << "\n"
              << "succeeded=" << (apiSucceeded ? "true" : "false") << "\n"
              << "topology_ok=" << (topoOk ? "true" : "false") << "\n"
              << "validation_ok=" << (validationOk ? "true" : "false") << "\n"
              << "artifact_dir=" << fs::absolute(caseDir).string() << "\n";
    return (apiSucceeded && topoOk && validationOk) ? 0 : 2;
}
