#!/usr/bin/python3

import argparse, yaml, tempfile, os, subprocess, json, jinja2, datetime, copy, re, dnf, pprint
import concurrent.futures
import rpm_showme as showme


# Features of this new release
# - multiarch from the ground up!
# - more resilient
# - better internal data structure
# - user-defined views


###############################################################################
### Help ######################################################################
###############################################################################


# Configs:
#   TYPE:           KEY:          ID:
# - repo            repos         repo_id
# - env_conf        envs          env_id
# - workload_conf   workloads     workload_id
# - label           labels        label_id
# - conf_view       views         view_id
#
# Data:
#   TYPE:         KEY:                 ID:
# - pkg           pkgs/repo_id/arch    NEVR
# - env           envs                 env_id:repo_id:arch_id
# - workload      workloads            workload_id:env_id:repo_id:arch_id
# - view          views                view_id:repo_id:arch_id
#
#
#



###############################################################################
### Some initial stuff ########################################################
###############################################################################

# Error in global settings for Feedback Pipeline
# Settings to be implemented, now hardcoded below
class SettingsError(Exception):
    pass

# Error in user-provided configs
class ConfigError(Exception):
    pass

# Error in downloading repodata
class RepoDownloadError(Exception):
    pass


def log(msg):
    print(msg)

def err_log(msg):
    print("ERROR LOG:  {}".format(msg))

def dump_data(path, data):
    with open(path, 'w') as file:
        json.dump(data, file)


def load_data(path):
    with open(path, 'r') as file:
        data = json.load(file)
    return data

def size(num, suffix='B'):
    for unit in ['','k','M','G']:
        if abs(num) < 1024.0:
            return "%3.1f %s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f %s%s" % (num, 'T', suffix)

def load_settings():
    settings = {}

    parser = argparse.ArgumentParser()
    parser.add_argument("configs", help="Directory with YAML configuration files. Only files ending with '.yaml' are accepted.")
    parser.add_argument("output", help="Directory to contain the output.")
    args = parser.parse_args()

    settings["configs"] = args.configs
    settings["output"] = args.output

    # FIXME: This is hardcorded, and it shouldn't be!
    #settings["allowed_arches"] = ["armv7hl","aarch64","i686","ppc64le","s390x","x86_64"]
    # FIXME Limiting arches for faster results during development
    settings["allowed_arches"] = ["armv7hl","aarch64","ppc64le","s390x","x86_64"]
    return(settings)




###############################################################################
### Loading user-provided configs #############################################
###############################################################################

# Configs:
#   TYPE:         KEY:          ID:
# - repo          repos         repo_id
# - env           envs          env_id
# - workload      workloads     workload_id
# - label         labels        label_id
# - view          views         view_id

def _load_config_repo(document_id, document, settings):
    config = {}
    config["id"] = document_id

    # Step 1: Mandatory fields
    try:
        # Name is an identifier for humans
        config["name"] = str(document["data"]["name"])

        # A short description, perhaps hinting the purpose
        config["description"] = str(document["data"]["description"])

        # Who maintains it? This is just a freeform string
        # for humans to read. In Fedora, a FAS nick is recommended.
        config["maintainer"] = str(document["data"]["maintainer"])

        # Where does this repository come from?
        # Right now, only Fedora repositories are supported,
        # defined by their releasever.
        config["source"] = {}

        # Only Fedora repos supported at this time.
        # Fedora release.
        config["source"]["fedora_release"] = str(document["data"]["source"]["fedora_release"])

        # List of architectures
        config["source"]["architectures"] = []
        for arch_raw in document["data"]["source"]["architectures"]:
            arch = str(arch_raw)
            if arch not in settings["allowed_arches"]:
                err_log("Warning: {file}.yaml lists an invalid architecture: {arch}. Ignoring.".format(
                    file=document_id,
                    arch=arch))
                continue
            config["source"]["architectures"].append(str(arch))
    except KeyError:
        raise ConfigError("Error: {file} is invalid.".format(file=yml_file))
    
    # Step 2: Optional fields
    # none here

    return config


def _load_config_env(document_id, document, settings):
    config = {}
    config["id"] = document_id

    # Step 1: Mandatory fields
    try:
        # Name is an identifier for humans
        config["name"] = str(document["data"]["name"])

        # A short description, perhaps hinting the purpose
        config["description"] = str(document["data"]["description"])

        # Who maintains it? This is just a freeform string
        # for humans to read. In Fedora, a FAS nick is recommended.
        config["maintainer"] = str(document["data"]["maintainer"])

        # Different instances of the environment, one per repository.
        config["repositories"] = []
        for repo in document["data"]["repositories"]:
            config["repositories"].append(str(repo))
        
        # Packages defining this environment.
        # This list includes packages for all
        # architectures — that's the one to use by default.
        config["packages"] = []
        for pkg in document["data"]["packages"]:
            config["packages"].append(str(pkg))
        
        # Labels connect things together.
        # Workloads get installed in environments with the same label.
        # They also get included in views with the same label.
        config["labels"] = []
        for repo in document["data"]["labels"]:
            config["labels"].append(str(repo))

    except KeyError:
        raise ConfigError("Error: {file} is invalid.".format(file=yml_file))

    # Step 2: Optional fields

    # Architecture-specific packages.
    config["arch_packages"] = {}
    for arch in settings["allowed_arches"]:
        config["arch_packages"][arch] = []
    if "arch_packages" in document["data"]:
        for arch, pkgs in document["data"]["arch_packages"].items():
            if arch not in settings["allowed_arches"]:
                err_log("Warning: {file}.yaml lists an invalid architecture: {arch}. Ignoring.".format(
                    file=document_id,
                    arch=arch
                ))
                continue
            for pkg_raw in pkgs:
                pkg = str(pkg_raw)
                config["arch_packages"][arch].append(pkg)
    
    # Extra installation options.
    # The following are now supported:
    # - "include-docs" - include documentation packages
    # - "include-weak-deps" - automatically pull in "recommends" weak dependencies
    config["options"] = []
    if "options" in document["data"]:
        if "include-docs" in document["data"]["options"]:
            config["options"].append("include-docs")
        if "include-weak-deps" in document["data"]["options"]:
            config["options"].append("include-weak-deps")

    return config


def _load_config_workload(document_id, document, settings):
    config = {}
    config["id"] = document_id

    # Step 1: Mandatory fields
    try:
        # Name is an identifier for humans
        config["name"] = str(document["data"]["name"])

        # A short description, perhaps hinting the purpose
        config["description"] = str(document["data"]["description"])

        # Who maintains it? This is just a freeform string
        # for humans to read. In Fedora, a FAS nick is recommended.
        config["maintainer"] = str(document["data"]["maintainer"])

        # Packages defining this workload.
        # This list includes packages for all
        # architectures — that's the one to use by default.
        config["packages"] = []
        for pkg in document["data"]["packages"]:
            config["packages"].append(str(pkg))
        
        # Labels connect things together.
        # Workloads get installed in environments with the same label.
        # They also get included in views with the same label.
        config["labels"] = []
        for repo in document["data"]["labels"]:
            config["labels"].append(str(repo))

    except KeyError:
        raise ConfigError("Error: {file} is invalid.".format(file=yml_file))

    # Step 2: Optional fields

    # Architecture-specific packages.
    config["arch_packages"] = {}
    for arch in settings["allowed_arches"]:
        config["arch_packages"][arch] = []
    if "arch_packages" in document["data"]:
        for arch, pkgs in document["data"]["arch_packages"].items():
            if arch not in settings["allowed_arches"]:
                err_log("Error: {file}.yaml lists an invalid architecture: {arch}. Ignoring.".format(
                    file=document_id,
                    arch=arch
                ))
                continue
            for pkg_raw in pkgs:
                pkg = str(pkg_raw)
                config["arch_packages"][arch].append(pkg)
    
    # Extra installation options.
    # The following are now supported:
    # - "include-docs" - include documentation packages
    # - "include-weak-deps" - automatically pull in "recommends" weak dependencies
    config["options"] = []
    if "options" in document["data"]:
        if "include-docs" in document["data"]["options"]:
            config["options"].append("include-docs")
        if "include-weak-deps" in document["data"]["options"]:
            config["options"].append("include-weak-deps")

    return config


def _load_config_label(document_id, document, settings):
    config = {}
    config["id"] = document_id

    # Step 1: Mandatory fields
    try:
        # Name is an identifier for humans
        config["name"] = str(document["data"]["name"])

        # A short description, perhaps hinting the purpose
        config["description"] = str(document["data"]["description"])

        # Who maintains it? This is just a freeform string
        # for humans to read. In Fedora, a FAS nick is recommended.
        config["maintainer"] = str(document["data"]["maintainer"])

    except KeyError:
        raise ConfigError("Error: {file} is invalid.".format(file=yml_file))

    # Step 2: Optional fields
    # none here

    return config


def _load_config_compose_view(document_id, document, settings):
    config = {}
    config["id"] = document_id
    config["type"] = "compose"

    # Step 1: Mandatory fields
    try:
        # Name is an identifier for humans
        config["name"] = str(document["data"]["name"])

        # A short description, perhaps hinting the purpose
        config["description"] = str(document["data"]["description"])

        # Who maintains it? This is just a freeform string
        # for humans to read. In Fedora, a FAS nick is recommended.
        config["maintainer"] = str(document["data"]["maintainer"])

        # Labels connect things together.
        # Workloads get installed in environments with the same label.
        # They also get included in views with the same label.
        config["labels"] = []
        for repo in document["data"]["labels"]:
            config["labels"].append(str(repo))

        # Choose one repository that gets used as a source.
        config["repository"] = str(document["data"]["repository"])

    except KeyError:
        raise ConfigError("Error: {document_id}.yml is invalid.".format(document_id=document_id))

    # Step 2: Optional fields
    
    # Limit this view only to the following architectures
    config["architectures"] = []
    if "architectures" in document["data"]:
        for repo in document["data"]["architectures"]:
            config["architectures"].append(str(repo))
    
    # Limit this view only to the following pkgs
    config["unwanted_packages"] = []
    if "unwanted_packages" in document["data"]:
        for pkg in document["data"]["unwanted_packages"]:
            config["unwanted_packages"].append(str(pkg))

    # Architecture-specific packages.
    config["unwanted_arch_packages"] = {}
    for arch in settings["allowed_arches"]:
        config["unwanted_arch_packages"][arch] = []
    if "unwanted_arch_packages" in document["data"]:
        for arch, pkgs in document["data"]["unwanted_arch_packages"].items():
            if arch not in settings["allowed_arches"]:
                err_log("Error: {file}.yaml lists an invalid architecture: {arch}. Ignoring.".format(
                    file=document_id,
                    arch=arch
                ))
                continue
            for pkg_raw in pkgs:
                pkg = str(pkg_raw)
                config["unwanted_arch_packages"][arch].append(pkg)

    return config


def get_configs(settings):
    log("")
    log("###############################################################################")
    log("### Loading user-provided configs #############################################")
    log("###############################################################################")
    log("")

    directory = settings["configs"]

    if "allowed_arches" not in settings:
        err_log("System error: allowed_arches not configured")
        raise SettingsError
    
    if not settings["allowed_arches"]:
        err_log("System error: no allowed_arches not configured")
        raise SettingsError

    configs = {}

    configs["repos"] = {}
    configs["envs"] = {}
    configs["workloads"] = {}
    configs["labels"] = {}
    configs["views"] = {}

    # Step 1: Load all configs
    log("Loading config files...")
    for yml_file in os.listdir(directory):
        # Only accept yaml files
        if not yml_file.endswith(".yaml"):
            continue
        
        document_id = yml_file.split(".yaml")[0]

        try:
            with open(os.path.join(directory, yml_file), "r") as file:
                # Safely load the config
                try:
                    document = yaml.safe_load(file)
                except yaml.YAMLError as err:
                    raise ConfigError("Error loading a config '{filename}': {err}".format(
                                filename=yml_file,
                                err=err))
                
                # Only accept yaml files stating their purpose!
                if not ("document" in document and "version" in document):
                    raise ConfigError("Error: {file} is invalid.".format(file=yml_file))


                # === Case: Repository config ===
                if document["document"] == "feedback-pipeline-repository":
                    configs["repos"][document_id] = _load_config_repo(document_id, document, settings)

                # === Case: Environment config ===
                if document["document"] == "feedback-pipeline-environment":
                    configs["envs"][document_id] = _load_config_env(document_id, document, settings)

                # === Case: Workload config ===
                if document["document"] == "feedback-pipeline-workload":
                    configs["workloads"][document_id] = _load_config_workload(document_id, document, settings)
                
                # === Case: Label config ===
                if document["document"] == "feedback-pipeline-label":
                    configs["labels"][document_id] = _load_config_label(document_id, document, settings)

                # === Case: View config ===
                if document["document"] == "feedback-pipeline-compose-view":
                    configs["views"][document_id] = _load_config_compose_view(document_id, document, settings)


        except ConfigError as err:
            err_log("Config load error: {err}. Ignoring.".format(err=err))
            continue
    
    log("  Done!")
    log("")

    # Step 2: cross check configs for references and other validation
    log("  Validating configs...")
    # FIXME: Do this, please!
    log("  Warning: This is not implemented, yet!")
    log("           But there would be a traceback somewhere during runtime ")
    log("           if an error exists, so wrong outputs won't happen.")

    log("  Done!")
    log("")
    
    log("Done!  Loaded:")
    log("  - {} repositories".format(len(configs["repos"])))
    log("  - {} environments".format(len(configs["envs"])))
    log("  - {} workloads".format(len(configs["workloads"])))
    log("  - {} labels".format(len(configs["labels"])))
    log("  - {} views".format(len(configs["views"])))
    log("")

    return configs



###############################################################################
### Analyzing stuff! ##########################################################
###############################################################################

# Configs:
#   TYPE:           KEY:          ID:
# - repo            repos         repo_id
# - env_conf        envs          env_id
# - workload_conf   workloads     workload_id
# - label           labels        label_id
# - conf_view       views         view_id
#
# Data:
#   TYPE:         KEY:                 ID:
# - pkg           pkgs/repo_id/arch    NEVR
# - env           envs                 env_id:repo_id:arch_id
# - workload      workloads            workload_id:env_id:repo_id:arch_id
# - view          views                view_id:repo_id:arch_id
#
# tmp contents:
# - dnf_cachedir-{repo}-{arch}
# - dnf_generic_installroot-{repo}-{arch}
# - dnf_env_installroot-{env_conf}-{repo}-{arch}
#
#

def _analyze_pkgs(tmp, repo, arch):
    log("Analyzing pkgs for {repo_name} ({repo_id}) {arch}".format(
            repo_name=repo["name"],
            repo_id=repo["id"],
            arch=arch
        ))
    
    with dnf.Base() as base:

        # Local DNF cache
        cachedir_name = "dnf_cachedir-{repo}-{arch}".format(
            repo=repo["id"],
            arch=arch
        )
        base.conf.cachedir = os.path.join(tmp, cachedir_name)

        # Generic installroot
        root_name = "dnf_generic_installroot-{repo}-{arch}".format(
            repo=repo["id"],
            arch=arch
        )
        base.conf.installroot = os.path.join(tmp, root_name)

        # Architecture
        base.conf.arch = arch
        base.conf.ignorearch = True

        # Repository
        base.conf.substitutions['releasever'] = repo["source"]["fedora_release"]

        # Load repos
        log("  Loading repos...")
        base.read_all_repos()
        # This sometimes fails, so let's try at least N times
        # before totally giving up!
        MAX_TRIES = 10
        attempts = 0
        success = False
        while attempts < MAX_TRIES:
            try:
                base.fill_sack(load_system_repo=False)
                success = True
                break
            except dnf.exceptions.RepoError as err:
                attempts +=1
                log("  Failed to download repodata. Trying again!")
        if not success:
            err = "Failed to download repodata while analyzing repo '{repo_name} ({repo_id}) {arch}".format(
            repo_name=repo["name"],
            repo_id=repo["id"],
            arch=arch
            )
            err_log(err)
            raise RepoDownloadError(err)

        # DNF query
        query = base.sack.query

        # Get all packages
        all_pkgs_set = set(query())
        pkgs = {}
        for pkg_object in all_pkgs_set:
            pkg_nevra = "{name}-{evr}.{arch}".format(
                name=pkg_object.name,
                evr=pkg_object.evr,
                arch=pkg_object.arch)
            pkg = {}
            pkg["id"] = pkg_nevra
            pkg["name"] = pkg_object.name
            pkg["evr"] = pkg_object.evr
            pkg["arch"] = pkg_object.arch
            pkg["installsize"] = pkg_object.installsize
            pkg["description"] = pkg_object.description
            #pkg["provides"] = pkg_object.provides
            #pkg["requires"] = pkg_object.requires
            #pkg["recommends"] = pkg_object.recommends
            #pkg["suggests"] = pkg_object.suggests
            pkg["summary"] = pkg_object.summary
            pkg["source_name"] = pkg_object.source_name
            pkgs[pkg_nevra] = pkg
        
        log("  Done!  ({pkg_count} packages in total)".format(
            pkg_count=len(pkgs)
        ))
        log("")

    return pkgs


def _analyze_env(tmp, env_conf, repo, arch):
    env = {}
    
    env["env_conf_id"] = env_conf["id"]
    env["pkg_ids"] = []
    env["repo_id"] = repo["id"]
    env["arch"] = arch

    env["errors"] = {}
    env["errors"]["non_existing_pkgs"] = []

    env["succeeded"] = True

    with dnf.Base() as base:

        # Local DNF cache
        cachedir_name = "dnf_cachedir-{repo}-{arch}".format(
            repo=repo["id"],
            arch=arch
        )
        base.conf.cachedir = os.path.join(tmp, cachedir_name)

        # Environment installroot
        root_name = "dnf_env_installroot-{env_conf}-{repo}-{arch}".format(
            env_conf=env_conf["id"],
            repo=repo["id"],
            arch=arch
        )
        base.conf.installroot = os.path.join(tmp, root_name)

        # Architecture
        base.conf.arch = arch
        base.conf.ignorearch = True

        # Repository
        base.conf.substitutions['releasever'] = repo["source"]["fedora_release"]

        # Additional DNF Settings
        base.conf.tsflags.append('justdb')

        # Environment config
        if "include-weak-deps" not in env_conf["options"]:
            base.conf.install_weak_deps = False
        if "include-docs" not in env_conf["options"]:
            base.conf.tsflags.append('nodocs')

        # Load repos
        log("  Loading repos...")
        base.read_all_repos()
        # This sometimes fails, so let's try at least N times
        # before totally giving up!
        MAX_TRIES = 10
        attempts = 0
        success = False
        while attempts < MAX_TRIES:
            try:
                base.fill_sack(load_system_repo=False)
                success = True
                break
            except dnf.exceptions.RepoError as err:
                attempts +=1
                log("  Failed to download repodata. Trying again!")
        if not success:
            err = "Failed to download repodata while analyzing environment '{env_conf}' from '{repo}' {arch}:".format(
                env_conf=env_conf["id"],
                repo=repo["id"],
                arch=arch
            )
            err_log(err)
            raise RepoDownloadError(err)


        # Packages
        log("  Adding packages...")
        for pkg in env_conf["packages"]:
            try:
                base.install(pkg)
            except dnf.exceptions.MarkingError:
                env["errors"]["non_existing_pkgs"].append(pkg)
                continue

        # Architecture-specific packages
        for pkg in env_conf["arch_packages"][arch]:
            try:
                base.install(pkg)
            except dnf.exceptions.MarkingError:
                env["errors"]["non_existing_pkgs"].append(pkg)
                continue
        
        # Resolve dependencies
        log("  Resolving dependencies...")
        try:
            base.resolve()
        except dnf.exceptions.DepsolveError as err:
            err_log("Failed to analyze environment '{env_conf}' from '{repo}' {arch}:".format(
                    env_conf=env_conf["id"],
                    repo=repo["id"],
                    arch=arch
                ))
            err_log("  - {err}".format(err=err))
            env["succeeded"] = False
            env["errors"]["message"] = str(err)
            return env

        # Write the result into RPMDB.
        # The transaction needs us to download all the packages. :(
        # So let's do that to make it happy.
        log("  Downloading packages...")
        base.download_packages(base.transaction.install_set)
        log("  Running DNF transaction, writing RPMDB...")
        try:
            base.do_transaction()
        except dnf.exceptions.TransactionCheckError as err:
            err_log("Failed to analyze environment '{env_conf}' from '{repo}' {arch}:".format(
                    env_conf=env_conf["id"],
                    repo=repo["id"],
                    arch=arch
                ))
            err_log("  - {err}".format(err=err))
            env["succeeded"] = False
            env["errors"]["message"] = str(err)
            return env

        # DNF Query
        log("  Creating a DNF Query object...")
        query = base.sack.query().filterm(pkg=base.transaction.install_set)

        for pkg in query:
            pkg_id = "{name}-{evr}.{arch}".format(
                name=pkg.name,
                evr=pkg.evr,
                arch=pkg.arch
            )
            env["pkg_ids"].append(pkg_id)
        
        log("  Done!  ({pkg_count} packages in total)".format(
            pkg_count=len(env["pkg_ids"])
        ))
        log("")
    
    return env


def _analyze_envs(tmp, configs):
    envs = {}

    # Look at all env configs...
    for env_conf_id, env_conf in configs["envs"].items():
        # For each of those, look at all repos it lists...
        for repo_id in env_conf["repositories"]:
            # And for each of the repo, look at all arches it supports.
            repo = configs["repos"][repo_id]
            for arch in repo["source"]["architectures"]:
                # Now it has
                #    all env confs *
                #    repos each config lists *
                #    archeas each repo supports
                # Analyze all of that!
                log("Analyzing {env_name} ({env_id}) from {repo_name} ({repo}) {arch}...".format(
                    env_name=env_conf["name"],
                    env_id=env_conf_id,
                    repo_name=repo["name"],
                    repo=repo_id,
                    arch=arch
                ))

                env_id = "{env_conf_id}:{repo_id}:{arch}".format(
                    env_conf_id=env_conf_id,
                    repo_id=repo_id,
                    arch=arch
                )
                envs[env_id] = _analyze_env(tmp, env_conf, repo, arch)
                
    
    return envs

def _analyze_package_relations(dnf_query):
    relations = {}

    for pkg in dnf_query:
        pkg_id = "{name}-{evr}.{arch}".format(
            name=pkg.name,
            evr=pkg.evr,
            arch=pkg.arch
        )
        
        required_by = set()
        recommended_by = set()
        suggested_by = set()

        for dep_pkg in dnf_query.filter(requires=pkg.provides):
            dep_pkg_id = "{name}-{evr}.{arch}".format(
                name=dep_pkg.name,
                evr=dep_pkg.evr,
                arch=dep_pkg.arch
            )
            required_by.add(dep_pkg_id)

        for dep_pkg in dnf_query.filter(recommends=pkg.provides):
            dep_pkg_id = "{name}-{evr}.{arch}".format(
                name=dep_pkg.name,
                evr=dep_pkg.evr,
                arch=dep_pkg.arch
            )
            recommended_by.add(dep_pkg_id)
        
        for dep_pkg in dnf_query.filter(suggests=pkg.provides):
            dep_pkg_id = "{name}-{evr}.{arch}".format(
                name=dep_pkg.name,
                evr=dep_pkg.evr,
                arch=dep_pkg.arch
            )
            suggested_by.add(dep_pkg_id)
        
        relations[pkg_id] = {}
        relations[pkg_id]["required_by"] = sorted(list(required_by))
        relations[pkg_id]["recommended_by"] = sorted(list(recommended_by))
        relations[pkg_id]["suggested_by"] = sorted(list(suggested_by))
    
    return relations


    

def _analyze_workload(tmp, workload_conf, env_conf, repo, arch):
    workload = {}

    workload["workload_conf_id"] = workload_conf["id"]
    workload["env_conf_id"] = env_conf["id"]
    workload["repo_id"] = repo["id"]
    workload["arch"] = arch

    workload["pkg_env_ids"] = []
    workload["pkg_added_ids"] = []

    workload["pkg_relations"] = []

    workload["errors"] = {}
    workload["errors"]["non_existing_pkgs"] = []

    workload["succeeded"] = True

    with dnf.Base() as base:

        # Local DNF cache
        cachedir_name = "dnf_cachedir-{repo}-{arch}".format(
            repo=repo["id"],
            arch=arch
        )
        base.conf.cachedir = os.path.join(tmp, cachedir_name)

        # Environment installroot
        # Since we're not writing anything into the installroot,
        # let's just use the base image's installroot!
        root_name = "dnf_env_installroot-{env_conf}-{repo}-{arch}".format(
            env_conf=env_conf["id"],
            repo=repo["id"],
            arch=arch
        )
        base.conf.installroot = os.path.join(tmp, root_name)

        # Architecture
        base.conf.arch = arch
        base.conf.ignorearch = True

        # Repository
        base.conf.substitutions['releasever'] = repo["source"]["fedora_release"]

        # Environment config
        if "include-weak-deps" not in workload_conf["options"]:
            base.conf.install_weak_deps = False
        if "include-docs" not in workload_conf["options"]:
            base.conf.tsflags.append('nodocs')

        # Load repos
        log("  Loading repos...")
        base.read_all_repos()

        # Now I need to load the local RPMDB.
        # However, if the environment is empty, it wasn't created, so I need to treat
        # it differently. So let's check!
        if len(env_conf["packages"]) or len(env_conf["arch_packages"][arch]):
            # It's not empty! Load local data.
            base.fill_sack(load_system_repo=True)
        else:
            # It's empty. Treat it like we're using an empty installroot.
            # This sometimes fails, so let's try at least N times
            # before totally giving up!
            MAX_TRIES = 10
            attempts = 0
            success = False
            while attempts < MAX_TRIES:
                try:
                    base.fill_sack(load_system_repo=False)
                    success = True
                    break
                except dnf.exceptions.RepoError as err:
                    attempts +=1
                    log("  Failed to download repodata. Trying again!")
            if not success:
                err = "Failed to download repodata while analyzing workload '{workload_id} on '{env_id}' from '{repo}' {arch}...".format(
                        workload_id=workload_conf_id,
                        env_id=env_conf_id,
                        repo_name=repo["name"],
                        repo=repo_id,
                        arch=arch)
                err_log(err)
                raise RepoDownloadError(err)
        
        # Packages
        log("  Adding packages...")
        for pkg in workload_conf["packages"]:
            try:
                base.install(pkg)
            except dnf.exceptions.MarkingError:
                workload["errors"]["non_existing_pkgs"].append(pkg)
                continue

        # Architecture-specific packages
        for pkg in workload_conf["arch_packages"][arch]:
            try:
                base.install(pkg)
            except dnf.exceptions.MarkingError:
                workload["errors"]["non_existing_pkgs"].append(pkg)
                continue

        # Resolve dependencies
        log("  Resolving dependencies...")
        try:
            base.resolve()
        except dnf.exceptions.DepsolveError as err:
            workload["succeeded"] = False
            workload["errors"]["message"] = str(err)
            log("  Failed!  (Error message will be on the workload results page.")
            log("")
            return workload

        # DNF Query
        log("  Creating a DNF Query object...")
        query_env = base.sack.query()
        query_added = base.sack.query().filterm(pkg=base.transaction.install_set)
        pkgs_env = set(query_env.installed())
        pkgs_added = set(base.transaction.install_set)
        pkgs_all = set.union(pkgs_env, pkgs_added)
        query_all = base.sack.query().filterm(pkg=pkgs_all)
        
        for pkg in pkgs_env:
            pkg_id = "{name}-{evr}.{arch}".format(
                name=pkg.name,
                evr=pkg.evr,
                arch=pkg.arch
            )
            workload["pkg_env_ids"].append(pkg_id)
        
        for pkg in pkgs_added:
            pkg_id = "{name}-{evr}.{arch}".format(
                name=pkg.name,
                evr=pkg.evr,
                arch=pkg.arch
            )
            workload["pkg_added_ids"].append(pkg_id)
        
        workload["pkg_relations"] = _analyze_package_relations(query_all)
        
        pkg_env_count = len(workload["pkg_env_ids"])
        pkg_added_count = len(workload["pkg_added_ids"])
        log("  Done!  ({pkg_count} packages in total. That's {pkg_env_count} in the environment, and {pkg_added_count} added.)".format(
            pkg_count=str(pkg_env_count + pkg_added_count),
            pkg_env_count=pkg_env_count,
            pkg_added_count=pkg_added_count
        ))
        log("")

    return workload


def _analyze_workloads(tmp, configs):
    workloads = {}

    # Here, I need to mix and match workloads & envs based on labels
    workload_env_map = {}
    # Look at all workload configs...
    for workload_conf_id, workload_conf in configs["workloads"].items():
        workload_env_map[workload_conf_id] = set()
        # ... and all of their labels.
        for label in workload_conf["labels"]:
            # And for each label, find all env configs...
            for env_conf_id, env_conf in configs["envs"].items():
                # ... that also have the label.
                if label in env_conf["labels"]:
                    # And save those.
                    workload_env_map[workload_conf_id].add(env_conf_id)
    
    # Get the total number of workloads
    number_of_workloads = 0
    # And now, look at all workload configs...
    for workload_conf_id, workload_conf in configs["workloads"].items():
        # ... and for each, look at all env configs it should be analyzed in.
        for env_conf_id in workload_env_map[workload_conf_id]:
            # Each of those envs can have multiple repos associated...
            env_conf = configs["envs"][env_conf_id]
            for repo_id in env_conf["repositories"]:
                # ... and each repo probably has multiple architecture.
                repo = configs["repos"][repo_id]
                arches = repo["source"]["architectures"]
                number_of_workloads += len(arches)

    # Analyze the workloads
    current_workload = 0
    # And now, look at all workload configs...
    for workload_conf_id, workload_conf in configs["workloads"].items():
        # ... and for each, look at all env configs it should be analyzed in.
        for env_conf_id in workload_env_map[workload_conf_id]:
            # Each of those envs can have multiple repos associated...
            env_conf = configs["envs"][env_conf_id]
            for repo_id in env_conf["repositories"]:
                # ... and each repo probably has multiple architecture.
                repo = configs["repos"][repo_id]
                for arch in repo["source"]["architectures"]:

                    current_workload += 1
                    log ("[ workload {current} of {total} ]".format(
                        current=current_workload,
                        total=number_of_workloads
                    ))

                    # And now it has:
                    #   all workload configs *
                    #   all envs that match those *
                    #   all repos of those envs *
                    #   all arches of those repos.
                    # That's a lot of stuff! Let's analyze all of that!
                    log("Analyzing {workload_name} ({workload_id}) on {env_name} ({env_id}) from {repo_name} ({repo}) {arch}...".format(
                        workload_name=workload_conf["name"],
                        workload_id=workload_conf_id,
                        env_name=env_conf["name"],
                        env_id=env_conf_id,
                        repo_name=repo["name"],
                        repo=repo_id,
                        arch=arch
                    ))

                    workload_id = "{workload_conf_id}:{env_conf_id}:{repo_id}:{arch}".format(
                        workload_conf_id=workload_conf_id,
                        env_conf_id=env_conf_id,
                        repo_id=repo_id,
                        arch=arch
                    )

                    # DNF leaks memory and file descriptors :/
                    # 
                    # So, this workaround runs it in a subprocess that should have its resources
                    # freed when done!
                    with concurrent.futures.ProcessPoolExecutor(max_workers=1) as executor:
                        workloads[workload_id] = executor.submit(_analyze_workload,tmp, workload_conf, env_conf, repo, arch).result()

                    #workloads[workload_id] = _analyze_workload(tmp, workload_conf, env_conf, repo, arch)


    return workloads


def analyze_things(configs, settings):
    log("")
    log("###############################################################################")
    log("### Analyzing stuff! ##########################################################")
    log("###############################################################################")
    log("")

    data = {}

    data["pkgs"] = {}
    data["envs"] = {}
    data["workloads"] = {}
    data["views"] = {}

    with tempfile.TemporaryDirectory() as tmp:

        # FIXME temporary override
        tmp = "/tmp/fixed-tmp"

        # List of supported arches
        all_arches = settings["allowed_arches"]

        # Packages
        log("")
        log("=====  Analyzing Repos & Packages =====")
        log("")
        for _,repo in configs["repos"].items():
            repo_id = repo["id"]
            data["pkgs"][repo_id] = {}
            for arch in repo["source"]["architectures"]:
                data["pkgs"][repo_id][arch] = _analyze_pkgs(tmp, repo, arch)

        # Environments
        log("")
        log("=====  Analyzing Environments =====")
        log("")
        data["envs"] = _analyze_envs(tmp, configs)

        # Workloads
        log("")
        log("=====  Analyzing Workloads =====")
        log("")
        data["workloads"] = _analyze_workloads(tmp, configs)


    return data


###############################################################################
### Query gives an easy access to the data! ###################################
###############################################################################

class Query():
    def __init__(self, data, configs, settings):
        self.data = data
        self.configs = configs
        self.settings = settings
    
    def size(self, num, suffix='B'):
        for unit in ['','k','M','G']:
            if abs(num) < 1024.0:
                return "%3.1f %s%s" % (num, unit, suffix)
            num /= 1024.0
        return "%.1f %s%s" % (num, 'T', suffix)
        

    def workloads(self, workload_conf_id, env_conf_id, repo_id, arch, list_all=False, output_change=None):
        # accepts none in any argument, and in those cases, answers for all instances

        # It can output just one part of the id.
        # That's useful to, for example, list all arches associated with a workload_conf_id
        if output_change:
            list_all = True
            if output_change not in ["workload_conf_ids", "env_conf_ids", "repo_ids", "arches"]:
                raise ValueError('output_change must be one of: "workload_conf_ids", "env_conf_ids", "repo_ids", "arches"')

        matching_ids = set()

        # list considered workload_conf_ids
        if workload_conf_id:
            workload_conf_ids = [workload_conf_id]
        else:
            workload_conf_ids = self.configs["workloads"].keys()

        # list considered env_conf_ids
        if env_conf_id:
            env_conf_ids = [env_conf_id]
        else:
            env_conf_ids = self.configs["envs"].keys()
        
        # list considered repo_ids
        if repo_id:
            repo_ids = [repo_id]
        else:
            repo_ids = self.configs["repos"].keys()
            
        # list considered arches
        if arch:
            arches = [arch]
        else:
            arches = self.settings["allowed_arches"]
        
        # And now try looping through all of that, and return True on a first occurance
        # This is a terrible amount of loops. But most cases will have just one item
        # in most of those, anyway. No one is expected to run this method with
        # a "None" for every argument!
        for workload_conf_id in workload_conf_ids:
            for env_conf_id in env_conf_ids:
                for repo_id in repo_ids:
                    for arch in arches:
                        workload_id = "{workload_conf_id}:{env_conf_id}:{repo_id}:{arch}".format(
                            workload_conf_id=workload_conf_id,
                            env_conf_id=env_conf_id,
                            repo_id=repo_id,
                            arch=arch
                        )
                        if workload_id in self.data["workloads"].keys():
                            if not list_all:
                                return True
                            if output_change:
                                if output_change == "workload_conf_ids":
                                    matching_ids.add(workload_conf_id)
                                if output_change == "env_conf_ids":
                                    matching_ids.add(env_conf_id)
                                if output_change == "repo_ids":
                                    matching_ids.add(repo_id)
                                if output_change == "arches":
                                    matching_ids.add(arch)
                            else:
                                matching_ids.add(workload_id)
        
        if not list_all:
            return False
        return sorted(list(matching_ids))
    
    def workloads_id(self, id, list_all=False, output_change=None):
        # Accepts both env and workload ID, and returns workloads that match that
        id_components = id.split(":")

        # It's an env!
        if len(id_components) == 3:
            env_conf_id = id_components[0]
            repo_id = id_components[1]
            arch = id_components[2]
            return self.workloads(None, env_conf_id, repo_id, arch, list_all, output_change)
        
        # It's a workload! Why would you want that, anyway?!
        if len(id_components) == 4:
            workload_conf_id = id_components[0]
            env_conf_id = id_components[1]
            repo_id = id_components[2]
            arch = id_components[3]
            return self.workloads(workload_conf_id, env_conf_id, repo_id, arch, list_all, output_change)
        
        raise ValueError("That seems to be an invalid ID!")

    def envs(self, env_conf_id, repo_id, arch, list_all=False, output_change=None):
        # accepts none in any argument, and in those cases, answers for all instances

        # It can output just one part of the id.
        # That's useful to, for example, list all arches associated with a workload_conf_id
        if output_change:
            list_all = True
            if output_change not in ["env_conf_ids", "repo_ids", "arches"]:
                raise ValueError('output_change must be one of: "env_conf_ids", "repo_ids", "arches"')
        
        matching_ids = set()

        # list considered env_conf_ids
        if env_conf_id:
            env_conf_ids = [env_conf_id]
        else:
            env_conf_ids = self.configs["envs"].keys()
        
        # list considered repo_ids
        if repo_id:
            repo_ids = [repo_id]
        else:
            repo_ids = self.configs["repos"].keys()
            
        # list considered arches
        if arch:
            arches = [arch]
        else:
            arches = self.settings["allowed_arches"]
        
        # And now try looping through all of that, and return True on a first occurance
        # This is a terrible amount of loops. But most cases will have just one item
        # in most of those, anyway. No one is expected to run this method with
        # a "None" for every argument!
        for env_conf_id in env_conf_ids:
            for repo_id in repo_ids:
                for arch in arches:
                    env_id = "{env_conf_id}:{repo_id}:{arch}".format(
                        env_conf_id=env_conf_id,
                        repo_id=repo_id,
                        arch=arch
                    )
                    if env_id in self.data["envs"].keys():
                        if not list_all:
                            return True
                        if output_change:
                            if output_change == "env_conf_ids":
                                matching_ids.add(env_conf_id)
                            if output_change == "repo_ids":
                                matching_ids.add(repo_id)
                            if output_change == "arches":
                                matching_ids.add(arch)
                        else:
                            matching_ids.add(env_id)
        
        # This means nothing has been found!
        if not list_all:
            return False
        return sorted(list(matching_ids))
    
    def envs_id(self, id, list_all=False, output_change=None):
        # Accepts both env and workload ID, and returns workloads that match that
        id_components = id.split(":")

        # It's an env!
        if len(id_components) == 3:
            env_conf_id = id_components[0]
            repo_id = id_components[1]
            arch = id_components[2]
            return self.envs(env_conf_id, repo_id, arch, list_all, output_change)
        
        # It's a workload!
        if len(id_components) == 4:
            workload_conf_id = id_components[0]
            env_conf_id = id_components[1]
            repo_id = id_components[2]
            arch = id_components[3]
            return self.envs(env_conf_id, repo_id, arch, list_all, output_change)
        
        raise ValueError("That seems to be an invalid ID!")
    
    def workload_pkgs(self, workload_conf_id, env_conf_id, repo_id, arch, output_change=None):
        # Warning: mixing repos and arches works, but might cause mess on the output

        # Default output is just a flat list. Extra fields will be added into each package:
        # q_in          - set of workload_ids including this pkg
        # q_required_in - set of workload_ids where this pkg is required (top-level)
        # q_env_in      - set of workload_ids where this pkg is in env
        # q_arch        - architecture

        # Other outputs:
        #   - "ids"         — a list ids
        #   - "source_names"  — a list of SRPM names
        if output_change:
            list_all = True
            if output_change not in ["ids", "source_names"]:
                raise ValueError('output_change must be one of: "ids", "source_names"')
        
        # Step 1: get all the matching workloads!
        workload_ids = self.workloads(workload_conf_id, env_conf_id, repo_id, arch, list_all=True)

        # I'll need repo_ids and arches to access the packages
        repo_ids = self.workloads(workload_conf_id, env_conf_id, repo_id, arch, output_change="repo_ids")
        arches = self.workloads(workload_conf_id, env_conf_id, repo_id, arch, output_change="arches")

        # Replicating the same structure as in data["pkgs"]
        # That is: [repo_id][arch][pkg_id]
        pkgs = {}
        for repo_id in repo_ids:
            pkgs[repo_id] = {}
            for arch in arches:
                pkgs[repo_id][arch] = {}

        # Workloads are already paired with envs, repos, and arches
        # (there is one for each combination)
        for workload_id in workload_ids:
            workload = self.data["workloads"][workload_id]
            workload_arch = workload["arch"]
            workload_repo_id = workload["repo_id"]
            workload_conf_id = workload["workload_conf_id"]

            # First, get all pkgs in the env
            for pkg_id in workload["pkg_env_ids"]:

                # Add it to the list if it's not there already.
                # Create a copy since it's gonna be modified, and include only what's needed
                pkg = self.data["pkgs"][workload_repo_id][workload_arch][pkg_id]
                if pkg_id not in pkgs[workload_repo_id][workload_arch]:
                    pkgs[workload_repo_id][workload_arch][pkg_id] = {}
                    pkgs[workload_repo_id][workload_arch][pkg_id]["id"] = pkg_id
                    pkgs[workload_repo_id][workload_arch][pkg_id]["name"] = pkg["name"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["evr"] = pkg["evr"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["arch"] = pkg["arch"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["installsize"] = pkg["installsize"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["description"] = pkg["description"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["summary"] = pkg["summary"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["source_name"] = pkg["source_name"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_arch"] = workload_arch
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_in"] = set()
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_required_in"] = set()
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_env_in"] = set()
                
                # It's here, so add it
                pkgs[workload_repo_id][workload_arch][pkg_id]["q_in"].add(workload_id)
                # Browsing env packages, so add it
                pkgs[workload_repo_id][workload_arch][pkg_id]["q_env_in"].add(workload_id)
                # Is it required?
                if pkg["name"] in self.configs["workloads"][workload_conf_id]["packages"]:
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_required_in"].add(workload_id)
                if pkg["name"] in self.configs["workloads"][workload_conf_id]["arch_packages"][workload_arch]:
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_required_in"].add(workload_id)
            
            # Second, add all the other packages
            for pkg_id in workload["pkg_added_ids"]:

                # Add it to the list if it's not there already
                # and initialize extra fields
                pkg = self.data["pkgs"][workload_repo_id][workload_arch][pkg_id]
                if pkg_id not in pkgs[workload_repo_id][workload_arch]:
                    pkgs[workload_repo_id][workload_arch][pkg_id] = {}
                    pkgs[workload_repo_id][workload_arch][pkg_id]["id"] = pkg_id
                    pkgs[workload_repo_id][workload_arch][pkg_id]["name"] = pkg["name"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["evr"] = pkg["evr"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["arch"] = pkg["arch"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["installsize"] = pkg["installsize"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["description"] = pkg["description"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["summary"] = pkg["summary"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["source_name"] = pkg["source_name"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_arch"] = workload_arch
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_in"] = set()
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_required_in"] = set()
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_env_in"] = set()
                
                # It's here, so add it
                pkgs[workload_repo_id][workload_arch][pkg_id]["q_in"].add(workload_id)
                # Not adding it to q_env_in
                # Is it required?
                if pkg["name"] in self.configs["workloads"][workload_conf_id]["packages"]:
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_required_in"].add(workload_id)
                if pkg["name"] in self.configs["workloads"][workload_conf_id]["arch_packages"][workload_arch]:
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_required_in"].add(workload_id)

        # Is it supposed to only output ids?
        if output_change:
            pkg_names = set()
            for repo_id in repo_ids:
                for arch in arches:
                    for pkg_id, pkg in pkgs[repo_id][arch].items():
                        if output_change == "ids":
                            pkg_names.add(pkg["id"])
                        elif output_change == "source_names":
                            pkg_names.add(pkg["source_name"])
            
            names_sorted = sorted(list(pkg_names))
            return names_sorted
                        

        # And now I just need to flatten that dict and return all packages as a list
        final_pkg_list = []
        for repo_id in repo_ids:
            for arch in arches:
                for pkg_id, pkg in pkgs[repo_id][arch].items():
                    final_pkg_list.append(pkg)

        # And sort them by nevr which is their ID
        final_pkg_list_sorted = sorted(final_pkg_list, key=lambda k: k['id'])

        return final_pkg_list_sorted


    def workload_pkgs_id(self, id, output_change=None):
        # Accepts both env and workload ID, and returns pkgs for workloads that match
        id_components = id.split(":")

        # It's an env!
        if len(id_components) == 3:
            env_conf_id = id_components[0]
            repo_id = id_components[1]
            arch = id_components[2]
            return self.workload_pkgs(None, env_conf_id, repo_id, arch, output_change)
        
        # It's a workload!
        if len(id_components) == 4:
            workload_conf_id = id_components[0]
            env_conf_id = id_components[1]
            repo_id = id_components[2]
            arch = id_components[3]
            return self.workload_pkgs(workload_conf_id, env_conf_id, repo_id, arch, output_change)
        
        raise ValueError("That seems to be an invalid ID!")
    
    def env_pkgs(self, env_conf_id, repo_id, arch):
        # Warning: mixing repos and arches works, but might cause mess on the output

        # Output is just a flat list. Extra fields will be added into each package:
        # q_in          - set of env_ids including this pkg
        # q_required_in - set of env_ids where this pkg is required (top-level)
        # q_arch        - architecture

        
        # Step 1: get all the matching envs!
        env_ids = self.envs(env_conf_id, repo_id, arch, list_all=True)

        # I'll need repo_ids and arches to access the packages
        repo_ids = self.envs(env_conf_id, repo_id, arch, output_change="repo_ids")
        arches = self.envs(env_conf_id, repo_id, arch, output_change="arches")

        # Replicating the same structure as in data["pkgs"]
        # That is: [repo_id][arch][pkg_id]
        pkgs = {}
        for repo_id in repo_ids:
            pkgs[repo_id] = {}
            for arch in arches:
                pkgs[repo_id][arch] = {}

        # envs are already paired with repos, and arches
        # (there is one for each combination)
        for env_id in env_ids:
            env = self.data["envs"][env_id]
            env_arch = env["arch"]
            env_repo_id = env["repo_id"]
            env_conf_id = env["env_conf_id"]

            for pkg_id in env["pkg_ids"]:

                # Add it to the list if it's not there already.
                # Create a copy since it's gonna be modified, and include only what's needed
                pkg = self.data["pkgs"][env_repo_id][env_arch][pkg_id]
                if pkg_id not in pkgs[env_repo_id][env_arch]:
                    pkgs[env_repo_id][env_arch][pkg_id] = {}
                    pkgs[env_repo_id][env_arch][pkg_id]["id"] = pkg_id
                    pkgs[env_repo_id][env_arch][pkg_id]["name"] = pkg["name"]
                    pkgs[env_repo_id][env_arch][pkg_id]["evr"] = pkg["evr"]
                    pkgs[env_repo_id][env_arch][pkg_id]["arch"] = pkg["arch"]
                    pkgs[env_repo_id][env_arch][pkg_id]["installsize"] = pkg["installsize"]
                    pkgs[env_repo_id][env_arch][pkg_id]["description"] = pkg["description"]
                    pkgs[env_repo_id][env_arch][pkg_id]["summary"] = pkg["summary"]
                    pkgs[env_repo_id][env_arch][pkg_id]["source_name"] = pkg["source_name"]
                    pkgs[env_repo_id][env_arch][pkg_id]["q_arch"] = env_arch
                    pkgs[env_repo_id][env_arch][pkg_id]["q_in"] = set()
                    pkgs[env_repo_id][env_arch][pkg_id]["q_required_in"] = set()
                
                # It's here, so add it
                pkgs[env_repo_id][env_arch][pkg_id]["q_in"].add(env_id)
                # Is it required?
                if pkg["name"] in self.configs["envs"][env_conf_id]["packages"]:
                    pkgs[env_repo_id][env_arch][pkg_id]["q_required_in"].add(env_id)
                if pkg["name"] in self.configs["envs"][env_conf_id]["arch_packages"][env_arch]:
                    pkgs[env_repo_id][env_arch][pkg_id]["q_required_in"].add(env_id)

        # And now I just need to flatten that dict and return all packages as a list
        final_pkg_list = []
        for repo_id in repo_ids:
            for arch in arches:
                for pkg_id, pkg in pkgs[repo_id][arch].items():
                    final_pkg_list.append(pkg)

        # And sort them by nevr which is their ID
        final_pkg_list_sorted = sorted(final_pkg_list, key=lambda k: k['id'])

        return final_pkg_list_sorted
    
    def env_pkgs_id(self, id):
        # Accepts both env and workload ID, and returns pkgs for envs that match
        id_components = id.split(":")

        # It's an env!
        if len(id_components) == 3:
            env_conf_id = id_components[0]
            repo_id = id_components[1]
            arch = id_components[2]
            return self.env_pkgs(env_conf_id, repo_id, arch)
        
        # It's a workload!
        if len(id_components) == 4:
            workload_conf_id = id_components[0]
            env_conf_id = id_components[1]
            repo_id = id_components[2]
            arch = id_components[3]
            return self.env_pkgs(env_conf_id, repo_id, arch)
        
        raise ValueError("That seems to be an invalid ID!")

    def workload_size(self, workload_conf_id, env_conf_id, repo_id, arch):
        # A total size of a workload (or multiple combined!)
        pkgs = self.workload_pkgs(workload_conf_id, env_conf_id, repo_id, arch)
        size = 0
        for pkg in pkgs:
            size += pkg["installsize"]
        return size

    def env_size(self, env_conf_id, repo_id, arch):
        # A total size of an env (or multiple combined!)
        pkgs = self.env_pkgs(env_conf_id, repo_id, arch)
        size = 0
        for pkg in pkgs:
            size += pkg["installsize"]
        return size

    def workload_size_id(self, id):
        # Accepts both env and workload ID, and returns pkgs for envs that match
        id_components = id.split(":")

        # It's an env!
        if len(id_components) == 3:
            env_conf_id = id_components[0]
            repo_id = id_components[1]
            arch = id_components[2]
            return self.workload_size(None, env_conf_id, repo_id, arch)
        
        # It's a workload!
        if len(id_components) == 4:
            workload_conf_id = id_components[0]
            env_conf_id = id_components[1]
            repo_id = id_components[2]
            arch = id_components[3]
            return self.workload_size(workload_conf_id, env_conf_id, repo_id, arch)
        
        raise ValueError("That seems to be an invalid ID!")
    
    def env_size_id(self, id):
        # Accepts both env and workload ID, and returns pkgs for envs that match
        id_components = id.split(":")

        # It's an env!
        if len(id_components) == 3:
            env_conf_id = id_components[0]
            repo_id = id_components[1]
            arch = id_components[2]
            return self.env_size(env_conf_id, repo_id, arch)
        
        # It's a workload!
        if len(id_components) == 4:
            workload_conf_id = id_components[0]
            env_conf_id = id_components[1]
            repo_id = id_components[2]
            arch = id_components[3]
            return self.env_size(env_conf_id, repo_id, arch)
        
        raise ValueError("That seems to be an invalid ID!")
    
    def workload_url_slug(self, workload_conf_id, env_conf_id, repo_id, arch):
        slug = "{workload_conf_id}--{env_conf_id}--{repo_id}--{arch}".format(
            workload_conf_id=workload_conf_id,
            env_conf_id=env_conf_id,
            repo_id=repo_id,
            arch=arch
        )
        return slug
    
    def env_url_slug(self, env_conf_id, repo_id, arch):
        slug = "{env_conf_id}--{repo_id}--{arch}".format(
            env_conf_id=env_conf_id,
            repo_id=repo_id,
            arch=arch
        )
        return slug

    def workload_id_string(self, workload_conf_id, env_conf_id, repo_id, arch):
        slug = "{workload_conf_id}:{env_conf_id}:{repo_id}:{arch}".format(
            workload_conf_id=workload_conf_id,
            env_conf_id=env_conf_id,
            repo_id=repo_id,
            arch=arch
        )
        return slug
    
    def env_id_string(self, env_conf_id, repo_id, arch):
        slug = "{env_conf_id}:{repo_id}:{arch}".format(
            env_conf_id=env_conf_id,
            repo_id=repo_id,
            arch=arch
        )
        return slug
    
    def url_slug_id(self, any_id):
        return any_id.replace(":", "--")
    
    def workloads_in_view(self, view_conf_id, arch):
        view_conf = self.configs["views"][view_conf_id]
        repo_id = view_conf["repository"]
        labels = view_conf["labels"]
        
        if arch not in self.settings["allowed_arches"]:
            raise ValueError("Unsupported arch: {arch}".format(
                arch=arch
            ))
        
        if arch not in self.arches_in_view(view_conf_id):
            return []

        # First, get a set of workloads matching the repo and the arch
        too_many_workload_ids = set()
        workload_ids = self.workloads(None,None,repo_id,arch,list_all=True)
        too_many_workload_ids.update(workload_ids)

        # Second, limit that set further by matching the label
        final_workload_ids = set()
        for workload_id in too_many_workload_ids:
            workload = self.data["workloads"][workload_id]
            workload_conf_id = workload["workload_conf_id"]
            workload_conf = self.configs["workloads"][workload_conf_id]
            workload_labels = workload_conf["labels"]
            for workload_label in workload_labels:
                if workload_label in labels:
                    final_workload_ids.add(workload_id)

        return sorted(list(final_workload_ids))
    
    def arches_in_view(self, view_conf_id):

        if len(self.configs["views"][view_conf_id]["architectures"]):
            arches = self.configs["views"][view_conf_id]["architectures"]
            return sorted(arches)
        
        return self.settings["allowed_arches"]
    
    def pkgs_in_view(self, view_conf_id, arch, output_change=None):

        # Extra fields will be added into each package:
        # q_in          - set of workload_ids including this pkg
        # q_required_in - set of workload_ids where this pkg is required (top-level)
        # q_env_in      - set of workload_ids where this pkg is in env
        # q_dep_in      - set of workload_ids where this pkg is a dependency (that means not required)

        # Other outputs:
        #   - "ids"         — a list ids
        #   - "source_names"  — a list of SRPM names
        if output_change:
            list_all = True
            if output_change not in ["ids", "source_names"]:
                raise ValueError('output_change must be one of: "ids", "source_names"')

        workload_ids = self.workloads_in_view(view_conf_id, arch)
        repo_id = self.configs["views"][view_conf_id]["repository"]

        # This has just one repo and one arch, so a flat list of IDs is enough
        pkgs = {}
        
        for workload_id in workload_ids:
            workload = self.data["workloads"][workload_id]
            workload_conf_id = workload["workload_conf_id"]

            # First, get all pkgs in the env
            for pkg_id in workload["pkg_env_ids"]:
                # Add it to the list if it's not there already.
                # Create a copy since it's gonna be modified, and include only what's needed
                pkg = self.data["pkgs"][repo_id][arch][pkg_id]
                if pkg_id not in pkgs:
                    pkgs[pkg_id] = {}
                    pkgs[pkg_id]["id"] = pkg_id
                    pkgs[pkg_id]["name"] = pkg["name"]
                    pkgs[pkg_id]["evr"] = pkg["evr"]
                    pkgs[pkg_id]["arch"] = pkg["arch"]
                    pkgs[pkg_id]["installsize"] = pkg["installsize"]
                    pkgs[pkg_id]["description"] = pkg["description"]
                    pkgs[pkg_id]["summary"] = pkg["summary"]
                    pkgs[pkg_id]["source_name"] = pkg["source_name"]
                    pkgs[pkg_id]["q_arch"] = arch
                    pkgs[pkg_id]["q_in"] = set()
                    pkgs[pkg_id]["q_required_in"] = set()
                    pkgs[pkg_id]["q_dep_in"] = set()
                    pkgs[pkg_id]["q_env_in"] = set()
                
                # It's here, so add it
                pkgs[pkg_id]["q_in"].add(workload_id)
                # Browsing env packages, so add it
                pkgs[pkg_id]["q_env_in"].add(workload_id)
                # Is it required?
                if pkg["name"] in self.configs["workloads"][workload_conf_id]["packages"]:
                    pkgs[pkg_id]["q_required_in"].add(workload_id)
                if pkg["name"] in self.configs["workloads"][workload_conf_id]["arch_packages"][arch]:
                    pkgs[pkg_id]["q_required_in"].add(workload_id)

            # Second, add all the other packages
            for pkg_id in workload["pkg_added_ids"]:

                # Add it to the list if it's not there already
                # and initialize extra fields
                pkg = self.data["pkgs"][repo_id][arch][pkg_id]
                if pkg_id not in pkgs:
                    pkgs[pkg_id] = {}
                    pkgs[pkg_id]["id"] = pkg_id
                    pkgs[pkg_id]["name"] = pkg["name"]
                    pkgs[pkg_id]["evr"] = pkg["evr"]
                    pkgs[pkg_id]["arch"] = pkg["arch"]
                    pkgs[pkg_id]["installsize"] = pkg["installsize"]
                    pkgs[pkg_id]["description"] = pkg["description"]
                    pkgs[pkg_id]["summary"] = pkg["summary"]
                    pkgs[pkg_id]["source_name"] = pkg["source_name"]
                    pkgs[pkg_id]["q_arch"] = arch
                    pkgs[pkg_id]["q_in"] = set()
                    pkgs[pkg_id]["q_required_in"] = set()
                    pkgs[pkg_id]["q_dep_in"] = set()
                    pkgs[pkg_id]["q_env_in"] = set()
                
                # It's here, so add it
                pkgs[pkg_id]["q_in"].add(workload_id)
                # Not adding it to q_env_in
                # Is it required?
                if pkg["name"] in self.configs["workloads"][workload_conf_id]["packages"]:
                    pkgs[pkg_id]["q_required_in"].add(workload_id)
                elif pkg["name"] in self.configs["workloads"][workload_conf_id]["arch_packages"][arch]:
                    pkgs[pkg_id]["q_required_in"].add(workload_id)
                else:
                    pkgs[pkg_id]["q_dep_in"].add(workload_id)

        # Is it supposed to only output ids?
        if output_change:
            pkg_names = set()
            for pkg_id, pkg in pkgs.items():
                if output_change == "ids":
                    pkg_names.add(pkg["id"])
                elif output_change == "source_names":
                    pkg_names.add(pkg["source_name"])
            
            names_sorted = sorted(list(pkg_names))
            return names_sorted
                        

        # And now I just need to flatten that dict and return all packages as a list
        final_pkg_list = []
        for pkg_id, pkg in pkgs.items():
            final_pkg_list.append(pkg)

        # And sort them by nevr which is their ID
        final_pkg_list_sorted = sorted(final_pkg_list, key=lambda k: k['id'])

        return final_pkg_list_sorted
    
    def workload_succeeded(self, workload_conf_id, env_conf_id, repo_id, arch):
        workload_ids = self.workloads(workload_conf_id, env_conf_id, repo_id, arch, list_all=True)

        for workload_id in workload_ids:
            workload = self.data["workloads"][workload_id]
            if not workload["succeeded"]:
                return False
        return True
    
    def env_succeeded(self, env_conf_id, repo_id, arch):
        env_ids = self.envs(env_conf_id, repo_id, arch, list_all=True)

        for env_id in env_ids:
            env = self.data["envs"][env_id]
            if not env["succeeded"]:
                return False
        return True
    





###############################################################################
### Generating html pages! ####################################################
###############################################################################


def _generate_html_page(template_name, template_data, page_name, settings):
    log("Generating the '{page_name}' page...".format(
        page_name=page_name
    ))

    output = settings["output"]

    template_loader = jinja2.FileSystemLoader(searchpath="./templates/")
    template_env = jinja2.Environment(loader=template_loader)

    template = template_env.get_template("{template_name}.html".format(
        template_name=template_name
    ))

    if template_data:
        page = template.render(**template_data)
    else:
        page = template.render()

    filename = ("{page_name}.html".format(
        page_name=page_name.replace(":", "--")
    ))

    log("  Writing file...  ({filename})".format(
        filename=filename
    ))
    with open(os.path.join(output, filename), "w") as file:
        file.write(page)
    
    log("  Done!")
    log("")


def _generate_workload_pages(query):
    log("Generating workload pages...")

    # Workload overview pages
    for workload_conf_id in query.workloads(None,None,None,None,output_change="workload_conf_ids"):
        for repo_id in query.workloads(workload_conf_id,None,None,None,output_change="repo_ids"):
            template_data = {
                "query": query,
                "workload_conf_id": workload_conf_id,
                "repo_id": repo_id
            }

            page_name = "workload-overview--{workload_conf_id}--{repo_id}".format(
                workload_conf_id=workload_conf_id,
                repo_id=repo_id
            )
            _generate_html_page("workload_overview", template_data, page_name, query.settings)
    
    # Workload detail pages
    for workload_id in query.workloads(None,None,None,None,list_all=True):
        workload = query.data["workloads"][workload_id]
        
        workload_conf_id = workload["workload_conf_id"]
        workload_conf = query.configs["workloads"][workload_conf_id]

        env_conf_id = workload["env_conf_id"]
        env_conf = query.configs["envs"][env_conf_id]

        repo_id = workload["repo_id"]
        repo = query.configs["repos"][repo_id]


        template_data = {
            "query": query,
            "workload_id": workload_id,
            "workload": workload,
            "workload_conf": workload_conf,
            "env_conf": env_conf,
            "repo": repo
        }

        page_name = "workload--{workload_id}".format(
            workload_id=workload_id
        )
        _generate_html_page("workload", template_data, page_name, query.settings)
        page_name = "workload-dependencies--{workload_id}".format(
            workload_id=workload_id
        )
        _generate_html_page("workload_dependencies", template_data, page_name, query.settings)
    
    # Workload compare arches pages
    for workload_conf_id in query.workloads(None,None,None,None,output_change="workload_conf_ids"):
        for env_conf_id in query.workloads(workload_conf_id,None,None,None,output_change="env_conf_ids"):
            for repo_id in query.workloads(workload_conf_id,env_conf_id,None,None,output_change="repo_ids"):

                arches = query.workloads(workload_conf_id,env_conf_id,repo_id,None,output_change="arches")

                workload_conf = query.configs["workloads"][workload_conf_id]
                env_conf = query.configs["envs"][env_conf_id]
                repo = query.configs["repos"][repo_id]

                columns = {}
                rows = set()
                for arch in arches:
                    columns[arch] = {}

                    pkgs = query.workload_pkgs(workload_conf_id,env_conf_id,repo_id,arch)
                    for pkg in pkgs:
                        name = pkg["name"]
                        rows.add(name)
                        columns[arch][name] = pkg

                template_data = {
                    "query": query,
                    "workload_conf_id": workload_conf_id,
                    "workload_conf": workload_conf,
                    "env_conf_id": env_conf_id,
                    "env_conf": env_conf,
                    "repo_id": repo_id,
                    "repo": repo,
                    "columns": columns,
                    "rows": rows
                }

                page_name = "workload-cmp-arches--{workload_conf_id}--{env_conf_id}--{repo_id}".format(
                    workload_conf_id=workload_conf_id,
                    env_conf_id=env_conf_id,
                    repo_id=repo_id
                )

                _generate_html_page("workload_cmp_arches", template_data, page_name, query.settings)
    
    # Workload compare envs pages
    for workload_conf_id in query.workloads(None,None,None,None,output_change="workload_conf_ids"):
        for repo_id in query.workloads(workload_conf_id,None,None,None,output_change="repo_ids"):
            for arch in query.workloads(workload_conf_id,None,repo_id,None,output_change="arches"):

                env_conf_ids = query.workloads(workload_conf_id,None,repo_id,arch,output_change="env_conf_ids")

                workload_conf = query.configs["workloads"][workload_conf_id]
                repo = query.configs["repos"][repo_id]

                columns = {}
                rows = set()
                for env_conf_id in env_conf_ids:
                    columns[env_conf_id] = {}

                    pkgs = query.workload_pkgs(workload_conf_id,env_conf_id,repo_id,arch)
                    for pkg in pkgs:
                        name = pkg["name"]
                        rows.add(name)
                        columns[env_conf_id][name] = pkg

                template_data = {
                    "query": query,
                    "workload_conf_id": workload_conf_id,
                    "workload_conf": workload_conf,
                    "repo_id": repo_id,
                    "repo": repo,
                    "arch": arch,
                    "columns": columns,
                    "rows": rows
                }

                page_name = "workload-cmp-envs--{workload_conf_id}--{repo_id}--{arch}".format(
                    workload_conf_id=workload_conf_id,
                    repo_id=repo_id,
                    arch=arch
                )

                _generate_html_page("workload_cmp_envs", template_data, page_name, query.settings)
    
    log("  Done!")
    log("")


def _generate_env_pages(query):
    log("Generating env pages...")

    for env_conf_id in query.envs(None,None,None,output_change="env_conf_ids"):
        for repo_id in query.envs(env_conf_id,None,None,output_change="repo_ids"):
            template_data = {
                "query": query,
                "env_conf_id": env_conf_id,
                "repo_id": repo_id
            }

            page_name = "env-overview--{env_conf_id}--{repo_id}".format(
                env_conf_id=env_conf_id,
                repo_id=repo_id
            )
            _generate_html_page("env_overview", template_data, page_name, query.settings)
    
    # env detail pages
    for env_id in query.envs(None,None,None,list_all=True):
        env = query.data["envs"][env_id]

        env_conf_id = env["env_conf_id"]
        env_conf = query.configs["envs"][env_conf_id]

        repo_id = env["repo_id"]
        repo = query.configs["repos"][repo_id]

        template_data = {
            "query": query,
            "env_id": env_id,
            "env": env,
            "env_conf": env_conf,
            "repo": repo
        }

        page_name = "env--{env_id}".format(
            env_id=env_id
        )

        _generate_html_page("env", template_data, page_name, query.settings)
    
    # env compare arches pages
    for env_conf_id in query.envs(None,None,None,output_change="env_conf_ids"):
        for repo_id in query.envs(env_conf_id,None,None,output_change="repo_ids"):

            arches = query.envs(env_conf_id,repo_id,None,output_change="arches")

            env_conf = query.configs["envs"][env_conf_id]
            repo = query.configs["repos"][repo_id]

            columns = {}
            rows = set()
            for arch in arches:
                columns[arch] = {}

                pkgs = query.env_pkgs(env_conf_id,repo_id,arch)
                for pkg in pkgs:
                    name = pkg["name"]
                    rows.add(name)
                    columns[arch][name] = pkg

            template_data = {
                "query": query,
                "env_conf_id": env_conf_id,
                "env_conf": env_conf,
                "repo_id": repo_id,
                "repo": repo,
                "columns": columns,
                "rows": rows
            }

            page_name = "env-cmp-arches--{env_conf_id}--{repo_id}".format(
                env_conf_id=env_conf_id,
                repo_id=repo_id
            )

            _generate_html_page("env_cmp_arches", template_data, page_name, query.settings)

    log("  Done!")
    log("")


def _generate_config_pages(query):
    log("Generating config pages...")

    for conf_type in ["repos", "envs", "workloads", "labels", "views"]:
        template_data = {
            "query": query,
            "conf_type": conf_type
        }
        page_name = "configs_{conf_type}".format(
            conf_type=conf_type
        )
        _generate_html_page("configs", template_data, page_name, query.settings)

    # Config repo pages
    for repo_id,repo_conf in query.configs["repos"].items():
        template_data = {
            "query": query,
            "repo_conf": repo_conf
        }
        page_name = "config-repo--{repo_id}".format(
            repo_id=repo_id
        )
        _generate_html_page("config_repo", template_data, page_name, query.settings)
    
    # Config env pages
    for env_conf_id,env_conf in query.configs["envs"].items():
        template_data = {
            "query": query,
            "env_conf": env_conf
        }
        page_name = "config-env--{env_conf_id}".format(
            env_conf_id=env_conf_id
        )
        _generate_html_page("config_env", template_data, page_name, query.settings)

    # Config workload pages
    for workload_conf_id,workload_conf in query.configs["workloads"].items():
        template_data = {
            "query": query,
            "workload_conf": workload_conf
        }
        page_name = "config-workload--{workload_conf_id}".format(
            workload_conf_id=workload_conf_id
        )
        _generate_html_page("config_workload", template_data, page_name, query.settings)

    # Config label pages
    for label_conf_id,label_conf in query.configs["labels"].items():
        template_data = {
            "query": query,
            "label_conf": label_conf
        }
        page_name = "config-label--{label_conf_id}".format(
            label_conf_id=label_conf_id
        )
        _generate_html_page("config_label", template_data, page_name, query.settings)

    # Config view pages
    for view_conf_id,view_conf in query.configs["views"].items():
        template_data = {
            "query": query,
            "view_conf": view_conf
        }
        page_name = "config-view--{view_conf_id}".format(
            view_conf_id=view_conf_id
        )
        _generate_html_page("config_view", template_data, page_name, query.settings)

    log("  Done!")
    log("")

def _generate_repo_pages(query):
    log("Generating repo pages...")

    for repo_id, repo in query.configs["repos"].items():
        for arch in repo["source"]["architectures"]:
            template_data = {
                "query": query,
                "repo": repo,
                "arch": arch
            }
            page_name = "repo--{repo_id}--{arch}".format(
                repo_id=repo_id,
                arch=arch
            )
            _generate_html_page("repo", template_data, page_name, query.settings)


    log("  Done!")
    log("")


def _generate_view_pages(query):
    log("Generating view pages...")

    for view_conf_id,view_conf in query.configs["views"].items():
        if view_conf["type"] == "compose":

            # First, generate the overview page comparing all architectures
            log("  Generating 'compose' view overview {view_conf_id}".format(
                view_conf_id=view_conf_id
            ))

            repo_id = view_conf["repository"]

            # That page needs the number of binary and source packages for each architecture
            arch_pkg_counts = {}
            for arch in query.settings["allowed_arches"]:
                arch_pkg_counts[arch] = {}

                workload_ids = query.workloads_in_view(view_conf_id, arch=arch)

                pkg_ids = query.pkgs_in_view(view_conf_id, arch, output_change="ids")
                pkg_source_names = query.pkgs_in_view(view_conf_id, arch, output_change="source_names")
                
                arch_pkg_counts[arch]["pkg_ids"] = len(pkg_ids)
                arch_pkg_counts[arch]["source_pkg_names"] = len(pkg_source_names)

            template_data = {
                "query": query,
                "view_conf": view_conf,
                "arch_pkg_counts": arch_pkg_counts
            }
            page_name = "view--{view_conf_id}".format(
                view_conf_id=view_conf_id
            )
            _generate_html_page("view_compose_overview", template_data, page_name, query.settings)

            log("    Done!")
            log("")

            # Second, generate detail pages for each architecture
            for arch in query.arches_in_view(view_conf_id):
                # First, generate the overview page comparing all architectures
                log("  Generating 'compose' view {view_conf_id} for {arch}".format(
                    view_conf_id=view_conf_id,
                    arch=arch
                ))

                template_data = {
                    "query": query,
                    "view_conf": view_conf,
                    "arch": arch,

                }
                page_name = "view--{view_conf_id}--{arch}".format(
                    view_conf_id=view_conf_id,
                    arch=arch
                )
                _generate_html_page("view_compose_packages", template_data, page_name, query.settings)
                page_name = "view-reasons--{view_conf_id}--{arch}".format(
                    view_conf_id=view_conf_id,
                    arch=arch
                )
                _generate_html_page("view_compose_reasons", template_data, page_name, query.settings)
                page_name = "view-workloads--{view_conf_id}--{arch}".format(
                    view_conf_id=view_conf_id,
                    arch=arch
                )
                _generate_html_page("view_compose_workloads", template_data, page_name, query.settings)


    log("  Done!")
    log("")



def generate_pages(query):
    log("")
    log("###############################################################################")
    log("### Generating html pages! ####################################################")
    log("###############################################################################")
    log("")

    # Copy static files
    log("Copying static files...")
    src_static_dir = os.path.join("templates", "_static")
    output_static_dir = os.path.join(query.settings["output"])
    subprocess.run(["cp", "-R", src_static_dir, output_static_dir])
    log("  Done!")
    log("")

    # Generate the landing page
    _generate_html_page("homepage", None, "index", query.settings)

    # Generate the main menu page
    _generate_html_page("results", None, "results", query.settings)

    # Generate config pages
    _generate_config_pages(query)

    # Generate the top-level results pages
    template_data = {
        "query": query
    }
    _generate_html_page("repos", template_data, "repos", query.settings)
    _generate_html_page("envs", template_data, "envs", query.settings)
    _generate_html_page("workloads", template_data, "workloads", query.settings)
    _generate_html_page("labels", template_data, "labels", query.settings)
    _generate_html_page("views", template_data, "views", query.settings)

    # Generate repo pages
    _generate_repo_pages(query)

    # Generate env_overview pages
    _generate_env_pages(query)

    # Generate workload_overview pages
    _generate_workload_pages(query)

    # Generate view pages
    _generate_view_pages(query)

    # Generate the errors page
    template_data = {
        "query": query
    }
    _generate_html_page("errors", template_data, "errors", query.settings)

    

###############################################################################
### Historic Data #############################################################
###############################################################################

def _save_current_historic_data(query):
    log("Generating current historic data...")

    # Where to save it
    year = datetime.datetime.now().strftime("%Y")
    week = datetime.datetime.now().strftime("%W")
    filename = "historic_data-{year}-week_{week}.json".format(
        year=year,
        week=week
    )
    output_dir = os.path.join(query.settings["output"], "history")
    file_path = os.path.join(output_dir, filename)

    # What to save there
    history_data = {}
    history_data["date"] = str(datetime.datetime.now().strftime("%Y-%m-%d"))
    history_data["workloads"] = {}
    history_data["envs"] = {}
    history_data["repos"] = {}
    history_data["views"] = {}

    for workload_id in query.workloads(None,None,None,None,list_all=True):
        workload = query.data["workloads"][workload_id]

        if not workload["succeeded"]:
            continue

        workload_history = {}
        workload_history["size"] = query.workload_size_id(workload_id)
        workload_history["pkg_count"] = len(query.workload_pkgs_id(workload_id))

        history_data["workloads"][workload_id] = workload_history
    
    for env_id in query.envs(None,None,None,list_all=True):
        env = query.data["envs"][env_id]

        if not env["succeeded"]:
            continue

        env_history = {}
        env_history["size"] = query.env_size_id(env_id)
        env_history["pkg_count"] = len(query.env_pkgs_id(env_id))

        history_data["envs"][env_id] = env_history

    for repo_id in query.configs["repos"].keys():
        history_data["repos"][repo_id] = {}

        for arch, pkgs in query.data["pkgs"][repo_id].items():

            repo_history = {}
            repo_history["pkg_count"] = len(pkgs)
            
            history_data["repos"][repo_id][arch] = repo_history
    
    for view_conf_id in query.configs["views"].keys():
        history_data["views"][view_conf_id] = {}

        for arch in query.arches_in_view(view_conf_id):

            pkg_ids = query.pkgs_in_view(view_conf_id, arch)

            view_history = {}
            view_history["pkg_count"] = len(pkg_ids)
            
            history_data["views"][view_conf_id][arch] = view_history

    # And save it
    log("  Saving in: {file_path}".format(
        file_path=file_path
    ))
    dump_data(file_path, history_data)

    log("  Done!")
    log("")


def _read_historic_data(query):
    log("Reading historic data...")

    directory = os.path.join(query.settings["output"], "history")

    # Do some basic validation of the filename
    all_filenames = os.listdir(directory)
    valid_filenames = []
    for filename in all_filenames:
        if bool(re.match("historic_data-....-week_...json", filename)):
            valid_filenames.append(filename)
    valid_filenames.sort()

    # Get the data
    historic_data = {}

    for filename in valid_filenames:
        with open(os.path.join(directory, filename), "r") as file:
            try:
                document = json.load(file)

                date = datetime.datetime.strptime(document["date"],"%Y-%m-%d")
                year = date.strftime("%Y")
                week = date.strftime("%W")
                key = "{year}-week_{week}".format(
                    year=year,
                    week=week
                )
            except (KeyError, ValueError):
                err_log("Invalid file in historic data: {filename}. Ignoring.".format(
                    filename=filename
                ))
                continue

            historic_data[key] = document

    return historic_data

    log("  Done!")
    log("")


def _save_json_data_entry(entry_name, entry_data, settings):
    log("Generating data entry for {entry_name}".format(
        entry_name=entry_name
    ))

    output = settings["output"]

    filename = ("{entry_name}.json".format(
        entry_name=entry_name.replace(":", "--")
    ))

    log("  Writing file...  ({filename})".format(
        filename=filename
    ))

    with open(os.path.join(output, filename), "w") as file:
        json.dump(entry_data, file)
    
    log("  Done!")
    log("")


def _generate_chartjs_data(historic_data, query):

    # Data for workload pages
    for workload_id in query.workloads(None, None, None, None, list_all=True):

        entry_data = {}

        # First, get the dates as chart labels
        entry_data["labels"] = []
        for _,entry in historic_data.items():
            date = entry["date"]
            entry_data["labels"].append(date)

        # Second, get the actual data for everything that's needed
        entry_data["datasets"] = []

        workload = query.data["workloads"][workload_id]
        workload_conf_id = workload["workload_conf_id"]
        workload_conf = query.configs["workloads"][workload_conf_id]

        dataset = {}
        dataset["data"] = []
        dataset["label"] = workload_conf["name"]
        dataset["fill"] = "false"

        for _,entry in historic_data.items():
            try:
                size = entry["workloads"][workload_id]["size"]

                # The chart needs the size in MB, but just as a number
                size_mb = "{0:.1f}".format(size/1024/1024)
                dataset["data"].append(size_mb)
            except KeyError:
                dataset["data"].append("null")

        entry_data["datasets"].append(dataset)

        entry_name = "chartjs-data--workload--{workload_id}".format(
            workload_id=workload_id
        )
        _save_json_data_entry(entry_name, entry_data, query.settings)
    
    # Data for workload overview pages
    for workload_conf_id in query.workloads(None,None,None,None,output_change="workload_conf_ids"):
        for repo_id in query.workloads(workload_conf_id,None,None,None,output_change="repo_ids"):

            entry_data = {}

            # First, get the dates as chart labels
            entry_data["labels"] = []
            for _,entry in historic_data.items():
                date = entry["date"]
                entry_data["labels"].append(date)

            # Second, get the actual data for everything that's needed
            entry_data["datasets"] = []

            for workload_id in query.workloads(workload_conf_id, None, repo_id, None, list_all=True):

                workload = query.data["workloads"][workload_id]
                env_conf_id = workload["env_conf_id"]
                env_conf = query.configs["envs"][env_conf_id]

                dataset = {}
                dataset["data"] = []
                dataset["label"] = "in {name} {arch}".format(
                    name=env_conf["name"],
                    arch=workload["arch"]
                )
                dataset["fill"] = "false"


                for _,entry in historic_data.items():
                    try:
                        size = entry["workloads"][workload_id]["size"]

                        # The chart needs the size in MB, but just as a number
                        size_mb = "{0:.1f}".format(size/1024/1024)
                        dataset["data"].append(size_mb)
                    except KeyError:
                        dataset["data"].append("null")

                entry_data["datasets"].append(dataset)

            entry_name = "chartjs-data--workload-overview--{workload_conf_id}--{repo_id}".format(
                workload_conf_id=workload_conf_id,
                repo_id=repo_id
            )
            _save_json_data_entry(entry_name, entry_data, query.settings)
    
    # Data for workload cmp arches pages
    for workload_conf_id in query.workloads(None,None,None,None,output_change="workload_conf_ids"):
        for env_conf_id in query.workloads(workload_conf_id,None,None,None,output_change="env_conf_ids"):
            for repo_id in query.workloads(workload_conf_id,env_conf_id,None,None,output_change="repo_ids"):

                workload_conf = query.configs["workloads"][workload_conf_id]
                env_conf = query.configs["envs"][env_conf_id]
                repo = query.configs["repos"][repo_id]

                entry_data = {}

                # First, get the dates as chart labels
                entry_data["labels"] = []
                for _,entry in historic_data.items():
                    date = entry["date"]
                    entry_data["labels"].append(date)

                # Second, get the actual data for everything that's needed
                entry_data["datasets"] = []

                for workload_id in query.workloads(workload_conf_id,env_conf_id,repo_id,None,list_all=True):

                    workload = query.data["workloads"][workload_id]
                    env_conf_id = workload["env_conf_id"]
                    env_conf = query.configs["envs"][env_conf_id]

                    dataset = {}
                    dataset["data"] = []
                    dataset["label"] = "{arch}".format(
                        arch=workload["arch"]
                    )
                    dataset["fill"] = "false"

                    for _,entry in historic_data.items():
                        try:
                            size = entry["workloads"][workload_id]["size"]

                            # The chart needs the size in MB, but just as a number
                            size_mb = "{0:.1f}".format(size/1024/1024)
                            dataset["data"].append(size_mb)
                        except KeyError:
                            dataset["data"].append("null")

                    entry_data["datasets"].append(dataset)

                entry_name = "chartjs-data--workload-cmp-arches--{workload_conf_id}--{env_conf_id}--{repo_id}".format(
                    workload_conf_id=workload_conf_id,
                    env_conf_id=env_conf_id,
                    repo_id=repo_id
                )
                _save_json_data_entry(entry_name, entry_data, query.settings)
    
    # Data for workload cmp envs pages
    for workload_conf_id in query.workloads(None,None,None,None,output_change="workload_conf_ids"):
        for repo_id in query.workloads(workload_conf_id,None,None,None,output_change="repo_ids"):
            for arch in query.workloads(workload_conf_id,None,repo_id,None,output_change="arches"):

                workload_conf = query.configs["workloads"][workload_conf_id]
                env_conf = query.configs["envs"][env_conf_id]
                repo = query.configs["repos"][repo_id]

                entry_data = {}

                # First, get the dates as chart labels
                entry_data["labels"] = []
                for _,entry in historic_data.items():
                    date = entry["date"]
                    entry_data["labels"].append(date)

                # Second, get the actual data for everything that's needed
                entry_data["datasets"] = []

                for workload_id in query.workloads(workload_conf_id,None,repo_id,arch,list_all=True):

                    workload = query.data["workloads"][workload_id]
                    repo = query.configs["repos"][repo_id]

                    dataset = {}
                    dataset["data"] = []
                    dataset["label"] = "{repo} {arch}".format(
                        repo=repo["name"],
                        arch=workload["arch"]
                    )
                    dataset["fill"] = "false"

                    for _,entry in historic_data.items():
                        try:
                            size = entry["workloads"][workload_id]["size"]

                            # The chart needs the size in MB, but just as a number
                            size_mb = "{0:.1f}".format(size/1024/1024)
                            dataset["data"].append(size_mb)
                        except KeyError:
                            dataset["data"].append("null")

                    entry_data["datasets"].append(dataset)

                entry_name = "chartjs-data--workload-cmp-envs--{workload_conf_id}--{repo_id}--{arch}".format(
                    workload_conf_id=workload_conf_id,
                    repo_id=repo_id,
                    arch=arch
                )
                _save_json_data_entry(entry_name, entry_data, query.settings)
    
    # Data for env pages
    for env_id in query.envs(None, None, None, list_all=True):

        entry_data = {}

        # First, get the dates as chart labels
        entry_data["labels"] = []
        for _,entry in historic_data.items():
            date = entry["date"]
            entry_data["labels"].append(date)

        # Second, get the actual data for everything that's needed
        entry_data["datasets"] = []

        env = query.data["envs"][env_id]
        env_conf_id = env["env_conf_id"]
        env_conf = query.configs["envs"][env_conf_id]

        dataset = {}
        dataset["data"] = []
        dataset["label"] = env_conf["name"]
        dataset["fill"] = "false"


        for _,entry in historic_data.items():
            try:
                size = entry["envs"][env_id]["size"]

                # The chart needs the size in MB, but just as a number
                size_mb = "{0:.1f}".format(size/1024/1024)
                dataset["data"].append(size_mb)
            except KeyError:
                dataset["data"].append("null")

        entry_data["datasets"].append(dataset)

        entry_name = "chartjs-data--env--{env_id}".format(
            env_id=env_id
        )
        _save_json_data_entry(entry_name, entry_data, query.settings)
    
    # Data for env overview pages
    for env_conf_id in query.envs(None,None,None,output_change="env_conf_ids"):
        for repo_id in query.envs(env_conf_id,None,None,output_change="repo_ids"):

            entry_data = {}

            # First, get the dates as chart labels
            entry_data["labels"] = []
            for _,entry in historic_data.items():
                date = entry["date"]
                entry_data["labels"].append(date)

            # Second, get the actual data for everything that's needed
            entry_data["datasets"] = []

            for env_id in query.envs(env_conf_id, repo_id, None, list_all=True):

                env = query.data["envs"][env_id]
                env_conf_id = env["env_conf_id"]
                env_conf = query.configs["envs"][env_conf_id]

                dataset = {}
                dataset["data"] = []
                dataset["label"] = "in {name} {arch}".format(
                    name=env_conf["name"],
                    arch=env["arch"]
                )
                dataset["fill"] = "false"


                for _,entry in historic_data.items():
                    try:
                        size = entry["envs"][env_id]["size"]

                        # The chart needs the size in MB, but just as a number
                        size_mb = "{0:.1f}".format(size/1024/1024)
                        dataset["data"].append(size_mb)
                    except KeyError:
                        dataset["data"].append("null")

                entry_data["datasets"].append(dataset)

            entry_name = "chartjs-data--env-overview--{env_conf_id}--{repo_id}".format(
                env_conf_id=env_conf_id,
                repo_id=repo_id
            )
            _save_json_data_entry(entry_name, entry_data, query.settings)
    
    # Data for env cmp arches pages
    for env_conf_id in query.envs(None,None,None,output_change="env_conf_ids"):
        for repo_id in query.envs(env_conf_id,None,None,output_change="repo_ids"):

            env_conf = query.configs["envs"][env_conf_id]
            env_conf = query.configs["envs"][env_conf_id]
            repo = query.configs["repos"][repo_id]

            entry_data = {}

            # First, get the dates as chart labels
            entry_data["labels"] = []
            for _,entry in historic_data.items():
                date = entry["date"]
                entry_data["labels"].append(date)

            # Second, get the actual data for everything that's needed
            entry_data["datasets"] = []

            for env_id in query.envs(env_conf_id,repo_id,None,list_all=True):

                env = query.data["envs"][env_id]

                dataset = {}
                dataset["data"] = []
                dataset["label"] = "{arch}".format(
                    arch=env["arch"]
                )
                dataset["fill"] = "false"

                for _,entry in historic_data.items():
                    try:
                        size = entry["envs"][env_id]["size"]

                        # The chart needs the size in MB, but just as a number
                        size_mb = "{0:.1f}".format(size/1024/1024)
                        dataset["data"].append(size_mb)
                    except KeyError:
                        dataset["data"].append("null")

                entry_data["datasets"].append(dataset)

            entry_name = "chartjs-data--env-cmp-arches--{env_conf_id}--{repo_id}".format(
                env_conf_id=env_conf_id,
                repo_id=repo_id
            )
            _save_json_data_entry(entry_name, entry_data, query.settings)
    
    # Data for compose view pages    
    for view_conf_id in query.configs["views"].keys():

        for arch in query.arches_in_view(view_conf_id):

            entry_data = {}

            # First, get the dates as chart labels
            entry_data["labels"] = []
            for _,entry in historic_data.items():
                date = entry["date"]
                entry_data["labels"].append(date)

            # Second, get the actual data for everything that's needed
            entry_data["datasets"] = []

            dataset = {}
            dataset["data"] = []
            dataset["label"] = "Number of packages"
            dataset["fill"] = "false"

            for _,entry in historic_data.items():
                try:
                    count = entry["views"][view_conf_id][arch]["pkg_count"]
                    dataset["data"].append(count)
                except KeyError:
                    dataset["data"].append("null")

            entry_data["datasets"].append(dataset)

            entry_name = "chartjs-data--view--{view_conf_id}--{arch}".format(
                view_conf_id=view_conf_id,
                arch=arch
            )
            _save_json_data_entry(entry_name, entry_data, query.settings)


def generate_historic_data(query):
    log("")
    log("###############################################################################")
    log("### Historic Data #############################################################")
    log("###############################################################################")
    log("")

    # Step 1: Save current data
    _save_current_historic_data(query)

    # Step 2: Read historic data
    historic_data = _read_historic_data(query)

    # Step 3: Generate Chart.js data
    _generate_chartjs_data(historic_data, query)
    


###############################################################################
### Main ######################################################################
###############################################################################

def run_create_cache():
    settings = load_settings()
    configs = get_configs(settings)
    data = analyze_things(configs, settings)

    dump_data("cache_settings.json", settings)
    dump_data("cache_configs.json", configs)
    dump_data("cache_data.json", data)

    query = Query(data, configs, settings)

    return query

def run_from_cache():
    settings = load_data("cache_settings.json")
    configs = load_data("cache_configs.json")
    data = load_data("cache_data.json")

    query = Query(data, configs, settings)

    return query


def main():

    time_started = datetime.datetime.now().strftime("%m/%d/%Y, %H:%M:%S")

    query = run_create_cache()
    #query = run_from_cache()

    generate_pages(query)
    generate_historic_data(query)

    time_ended = datetime.datetime.now().strftime("%m/%d/%Y, %H:%M:%S")

    log("")
    log("=============================")
    log("Feedback Pipeline build done!")
    log("=============================")
    log("")
    log("  Started:  {}".format(time_started))
    log("  Finished: {}".format(time_ended))
    log("")



def tests_to_be_made_actually_useful_at_some_point_because_this_is_terribble(query):


    print("")
    print("")
    print("")
    print("test test test")
    print("test test test")
    print("test test test")
    print("test test test")
    print("")
    print("")


    # does_workload_exist(self, workload_conf_id, env_conf_id, repo_id, arch):

    #   env-empty 
    #   env-minimal 
    #   label-eln-compose 
    #   label-fedora-31-all 
    #   label-fedora-rawhide-all 
    #   repo-fedora-31 
    #   repo-fedora-rawhide 
    #   view-eln-compose 
    #   workload-httpd 
    #   workload-nginx 
    #   x86_64
    #   aarch64

    print("Should be False:")
    print(query.workloads("mleko","mleko","mleko","mleko"))
    print("")

    print("Should be True:")
    print(query.workloads("workload-httpd","env-empty","repo-fedora-31","x86_64"))
    print("")

    print("Should be False:")
    print(query.workloads("workload-httpd","env-empty","repo-fedora-31","aarch64"))
    print("")

    print("Should be True:")
    print(query.workloads("workload-httpd","env-empty","repo-fedora-31",None))
    print("")

    print("Should be True:")
    print(query.workloads("workload-nginx",None, None, None))
    print("")

    print("Should be True:")
    print(query.workloads(None,"env-minimal",None,"x86_64"))
    print("")

    print("Should be True:")
    print(query.workloads(None,"env-minimal",None,"aarch64"))
    print("")

    print("Should be False:")
    print(query.workloads(None,"env-minimal","repo-fedora-31","x86_64"))
    print("")

    print("Should be False:")
    print(query.workloads(None,"env-minimal","repo-fedora-31","aarch64"))
    print("")

    print("----------")
    print("")
    print("")

    print("Should be 7:")
    print(len(query.workloads(None,None,None,None,list_all=True)))
    print("")

    print("Should be 3:")
    print(len(query.workloads(None,None,None,"aarch64",list_all=True)))
    print("")

    print("Should be 2:")
    print(len(query.workloads("workload-nginx",None,None,None,list_all=True)))
    print("")

    print("----------")
    print("")
    print("")

    print("Should be 2 workload-nginx:")
    for id in query.workloads("workload-nginx",None,None,None,list_all=True):
        print(id)
    print("")

    print("Should be all 7:")
    for id in query.workloads(None,None,None,None,list_all=True):
        print(id)
    print("")

    print("Should be all 6 rawhide:")
    for id in query.workloads(None,None,"repo-fedora-rawhide",None,list_all=True):
        print(id)
    print("")

    print("Should be all 2 empty rawhide:")
    for id in query.workloads(None,"env-empty","repo-fedora-rawhide",None,list_all=True):
        print(id)
    print("")

    print("Should be nothing:")
    for id in query.workloads("workload-nginx","env-empty","repo-fedora-rawhide",None,list_all=True):
        print(id)
    print("")

    print("----------")
    print("")
    print("")

    print("Should be env-empty:repo-fedora-31:x86_64")
    for id in query.envs_id("workload-httpd:env-empty:repo-fedora-31:x86_64", list_all=True):
        print(id)
    print("")

    print("Should be workload-httpd:env-empty:repo-fedora-31:x86_64")
    for id in query.workloads_id("workload-httpd:env-empty:repo-fedora-31:x86_64", list_all=True):
        print(id)
    print("")

    print("Should be two, workload-httpd:env-minimal:repo-fedora-rawhide:x86_64 and workload-nginx:...")
    for id in query.workloads_id("env-minimal:repo-fedora-rawhide:x86_64", list_all=True):
        print(id)
    print("")

    print("----------")
    print("")
    print("")

    print("Should be all 2 arches:")
    for id in query.workloads("workload-httpd",None, None,None,list_all=True,output_change="arches"):
        print(id)
    print("")

    print("Should be all 2 arches:")
    for id in query.workloads("workload-nginx",None, None,None,list_all=True,output_change="arches"):
        print(id)
    print("")

    print("Should be all 2 env_conf_ids:")
    for id in query.workloads("workload-httpd",None, None,None,list_all=True,output_change="env_conf_ids"):
        print(id)
    print("")

    print("Should be all 1 env_conf_id:")
    for id in query.workloads("workload-nginx",None, None,None,list_all=True,output_change="env_conf_ids"):
        print(id)
    print("")

    print("----------")
    print("")
    print("")

    print("Should be 104 packages:")
    pkgs = query.workload_pkgs("workload-nginx", "env-minimal", "repo-fedora-rawhide", "x86_64")
    print (len(pkgs))
    total = 0
    env = 0
    required = 0
    for pkg in pkgs:
        workload_id = "workload-nginx:env-minimal:repo-fedora-rawhide:x86_64"
        if workload_id in pkg["q_in"]:
            total += 1
        if workload_id in pkg["q_required_in"]:
            required += 1
        if workload_id in pkg["q_env_in"]:
            env +=1
    print("")
    print("Should be 104")
    print(total)
    print("Should be 22")
    print(env)
    print("Should be 1")
    print(required)
    print("")

    print("Should be 208 packages:")
    pkgs = query.workload_pkgs("workload-nginx", "env-minimal", "repo-fedora-rawhide", None)
    print (len(pkgs))
    total = 0
    env = 0
    required = 0
    for pkg in pkgs:
        workload_id = "workload-nginx:env-minimal:repo-fedora-rawhide:x86_64"
        if workload_id in pkg["q_in"]:
            total += 1
        if workload_id in pkg["q_required_in"]:
            required += 1
        if workload_id in pkg["q_env_in"]:
            env +=1
    print("")
    print("Should be 104")
    print(total)
    print("Should be 22")
    print(env)
    print("Should be 1")
    print(required)
    print("")
    print("")
    print("")

    print("----------")
    print("")
    print("")

    print("views!!!")
    print("")

    workload_ids = query.workloads_in_view("view-eln-compose", "x86_64")
    print("Should be 1:")
    print(len(workload_ids))
    print("")
    print("print should be one nginx")
    for workload_id in workload_ids:
        print(workload_id)


    print("")
    print("")
    print("Package Lists:")
    print("")
    print("")
    print("")
    package_ids1 = query.workload_pkgs_id("workload-httpd:env-empty:repo-fedora-rawhide:x86_64", output_change="ids")
    package_ids2 = query.workload_pkgs_id("workload-httpd:env-minimal:repo-fedora-rawhide:x86_64", output_change="ids")
    package_ids3 = query.workload_pkgs_id("workload-nginx:env-minimal:repo-fedora-rawhide:x86_64", output_change="ids")

    all_pkg_ids = set()

    all_pkg_ids.update(package_ids1)
    all_pkg_ids.update(package_ids2)
    all_pkg_ids.update(package_ids3)

    print(len(all_pkg_ids))

    pkg_ids = query.pkgs_in_view("view-eln-compose", "x86_64", output_change="ids")

    print(len(pkg_ids))





        # q_in          - set of workload_ids including this pkg
        # q_required_in - set of workload_ids where this pkg is required (top-level)
        # q_env_in      - set of workload_ids where this pkg is in env
        # size_text     - size in a human-readable format, like 6.5 MB



if __name__ == "__main__":
    main()
