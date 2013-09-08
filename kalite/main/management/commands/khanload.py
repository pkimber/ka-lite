"""
Utilities for downloading Khan Academy topic tree and 
massaging into data and files that we use in KA Lite.
"""

import copy
import datetime
import json
import os
import requests
import shutil
import sys
import time
from optparse import make_option

from django.core.management.base import BaseCommand, CommandError

import settings
from settings import LOG as logging
from utils.general import datediff
from utils import topic_tools

# get the path to an exercise file, so we can check, below, which ones exist
exercise_path = os.path.join(settings.PROJECT_PATH, "static/js/khan-exercises/exercises/%s.html")

slug_key = {
    "Topic": "node_slug",
    "Video": "readable_id",
    "Exercise": "name",
}

title_key = {
    "Topic": "title",
    "Video": "title",
    "Exercise": "display_name",
}

iconfilepath = "/images/power-mode/badges/"
iconextension = "-40x40.png"
defaulticon = "default"

attribute_whitelists = {
    "Topic": ["kind", "hide", "description", "id", "topic_page_url", "title", "extended_slug", "children", "node_slug", "in_knowledge_map", "y_pos", "x_pos", "icon_src"],
    "Video": ["kind", "description", "title", "duration", "keywords", "youtube_id", "download_urls", "readable_id"],
    "Exercise": ["kind", "description", "related_video_readable_ids", "display_name", "live", "name", "seconds_per_fast_problem", "prerequisites", "v_position", "h_position"]
}

kind_blacklist = [None, "Separator", "CustomStack", "Scratchpad", "Article"]

slug_blacklist = ["new-and-noteworthy", "talks-and-interviews", "coach-res", "partner-content", "cs"]

# blacklist of particular exercises that currently have problems being rendered
slug_blacklist += ["ordering_improper_fractions_and_mixed_numbers", "ordering_numbers", "conditional_statements_2"]

def download_khan_data(url, debug_cache_file=None, debug_cache_dir=settings.PROJECT_PATH + "../_khanload_cache"):
    """Download data from the given url.
    
    In DEBUG mode, these downloads are slow.  So for the sake of faster iteration,
    save the download to disk and re-serve it up again, rather than download again,
    if the file is less than a day old.
    """

    # Get the filename
    if not debug_cache_file:
        debug_cache_file = url.split("/")[-1] + ".json"

    # Create a directory to store these cached json files 
    if not os.path.exists(debug_cache_dir):
        os.mkdir(debug_cache_dir)
    debug_cache_file = os.path.join(debug_cache_dir, debug_cache_file)

    # Use the cache file if:
    # a) We're in DEBUG mode
    # b) The debug cache file exists
    # c) It's less than 7 days old.
    if settings.DEBUG and os.path.exists(debug_cache_file) and datediff(datetime.datetime.now(), datetime.datetime.fromtimestamp(os.path.getctime(debug_cache_file)), units="days") <= 14.0:
        # Slow to debug, so keep a local cache in the debug case only.
        sys.stdout.write("Using cached file: %s\n" % debug_cache_file)
        data = json.loads(open(debug_cache_file).read())
    else:
        sys.stdout.write("Downloading data from %s..." % url)
        sys.stdout.flush()
        data = json.loads(requests.get(url).content)
        sys.stdout.write("done.\n")
        # In DEBUG mode, store the debug cache file.
        if settings.DEBUG:
            with open(debug_cache_file, "w") as fh:
                fh.write(json.dumps(data))
    return data


def rebuild_topictree(data_path=settings.PROJECT_PATH + "/static/data/", remove_unknown_exercises=False):
    """Downloads topictree (and supporting) data from Khan Academy and uses it to
    rebuild the KA Lite topictree cache (topics.json).
    """

    topictree = download_khan_data("http://www.khanacademy.org/api/v1/topictree")

    related_exercise = {}  # Temp variable to save exercises related to particular videos

    def recurse_nodes(node, path=""):
        """
        Internal function for recursing over the topic tree, marking relevant metadata,
        and removing undesired attributes and children.
        """
        
        kind = node["kind"]

        # Only keep key data we can use
        for key in node.keys():
            if key not in attribute_whitelists[kind]:
                del node[key]

        # Fix up data
        if slug_key[kind] not in node:
            logging.warn("Could not find expected slug key (%s) on node: %s" % (slug_key[kind], node))
            node[slug_key[kind]] = node["id"]  # put it SOMEWHERE.
        node["slug"] = node[slug_key[kind]] if node[slug_key[kind]] != "root" else ""
        node["id"] = node["slug"]  # these used to be the same; now not. Easier if they stay the same (issue #233)

        node["path"] = path + topic_tools.kind_slugs[kind] + node["slug"] + "/"
        node["title"] = node[title_key[kind]]

        kinds = set([kind])

        # For each exercise, need to get related videos
        if kind == "Exercise":
            related_video_readable_ids = [vid["readable_id"] for vid in download_khan_data("http://www.khanacademy.org/api/v1/exercises/%s/videos" % node["name"], node["name"] + ".json")]
            node["related_video_readable_ids"] = related_video_readable_ids
            exercise = {
                "slug": node[slug_key[kind]],
                "title": node[title_key[kind]],
                "path": node["path"],
            }
            for video_id in node.get("related_video_readable_ids", []):
                related_exercise[video_id] = exercise

        # Recurse through children, remove any blacklisted items
        children_to_delete = []
        for i, child in enumerate(node.get("children", [])):
            child_kind = child.get("kind", None)
            if child_kind in kind_blacklist:
                children_to_delete.append(i)
                continue
            if child[slug_key[child_kind]] in slug_blacklist:
                children_to_delete.append(i)
                continue
            if child_kind == "Video" and set(["mp4", "png"]) - set(child.get("download_urls", {}).keys()):
                print "No download link for video: %s: %s ('%s')" % (child["youtube_id"], child["ka_url"], child["author_names"])
                # for now, since we expect the missing videos to be filled in soon, we won't skip these nodes
                # children_to_delete.append(i)
                # continue
            kinds = kinds.union(recurse_nodes(child, node["path"]))
        for i in reversed(children_to_delete):
            del node["children"][i]

        # Mark on topics whether they contain Videos, Exercises, or both
        if kind == "Topic":
            node["contains"] = list(kinds)

        return kinds
    recurse_nodes(topictree)


    # Limit exercises to only the previous list
    def recurse_nodes_to_delete_exercise(node):
        """
        Internal function for recursing the topic tree and removing new exercises.
        Requires rebranding of metadata done by recurse_nodes function.
        Returns a list of exercise slugs for the exercises that were deleted.
        """
        # Stop recursing when we hit leaves
        if node["kind"] != "Topic":
            return []

        slugs_deleted = []

        children_to_delete = []
        for ci, child in enumerate(node.get("children", [])):
            # Mark all unrecognized exercises for deletion
            if child["kind"] == "Exercise":
                if not os.path.exists(exercise_path % child["slug"]):
                    children_to_delete.append(ci)
                
            # Recurse over children to delete
            elif child.get("children", None):
                slugs_deleted += recurse_nodes_to_delete_exercise(child)
                # Delete children without children (all their children were removed)
                if not child.get("children", None):
                    logging.debug("Removing now-childless topic node '%s'" % child["slug"])
                    children_to_delete.append(ci)
                # If there are no longer exercises, be honest about it
                elif not any([ch["kind"] == "Exercise" or "Exercise" in ch.get("contains", []) for ch in child["children"]]):
                    child["contains"] = list(set(child["contains"]) - set(["Exercise"]))

        # Do the actual deletion
        for i in reversed(children_to_delete):
            logging.debug("Deleting unknown exercise %s" % node["children"][i]["slug"])
            del node["children"][i]
        
        return slugs_deleted
    
    if remove_unknown_exercises:
        slugs_deleted = recurse_nodes_to_delete_exercise(topictree) # do this before [add related]
        for vid, ex in related_exercise.items():
            if ex and ex["slug"] in slugs_deleted:
                related_exercise[vid] = None

    def recurse_nodes_to_add_related_exercise(node):
        """
        Internal function for recursing the topic tree and marking related exercises.
        Requires rebranding of metadata done by recurse_nodes function.
        """
        if node["kind"] == "Video":
            node["related_exercise"] = related_exercise.get(node["slug"], None)
        for child in node.get("children", []):
            recurse_nodes_to_add_related_exercise(child)
    recurse_nodes_to_add_related_exercise(topictree)

    def recurse_nodes_to_remove_childless_nodes(node):
        """
        When we remove exercises, we remove dead-end topics.
        Khan just sends us dead-end topics, too.
        Let's remove those too.
        """
        children_to_delete = []
        for ci, child in enumerate(node.get("children", [])):
            # Mark all unrecognized exercises for deletion
            if child["kind"] != "Topic":
                continue

            recurse_nodes_to_remove_childless_nodes(child)

            if not child.get("children"):
                children_to_delete.append(ci)
                logging.debug("Removing KA childless topic: %s" % child["slug"])

        for ci in reversed(children_to_delete):
            del node["children"][ci]
    recurse_nodes_to_remove_childless_nodes(topictree)


        # Do the actual deletion
    with open(os.path.join(data_path, topic_tools.topics_file), "w") as fp:
        fp.write(json.dumps(topictree, indent=2))

    return topictree


def rebuild_knowledge_map(topictree, node_cache, data_path=settings.PROJECT_PATH + "/static/data/", force_icons=False):
    """
    Uses KA Lite topic data and supporting data from Khan Academy 
    to rebuild the knowledge map (maplayout.json) and topicdata files.
    """

    knowledge_map = download_khan_data("http://www.khanacademy.org/api/v1/maplayout")
    knowledge_topics = {}  # Stored variable that keeps all exercises related to second-level topics

    def scrub_knowledge_map(knowledge_map, node_cache):
        """
        Some topics in the knowledge map, we don't keep in our topic tree / node cache.
        Eliminate them from the knowledge map here.
        """
        for slug in knowledge_map["topics"].keys():
            nodecache_node = node_cache["Topic"].get(slug)
            topictree_node = topic_tools.get_topic_by_path(node_cache["Topic"][slug]["path"], root_node=topictree)

            if not nodecache_node or not topictree_node:
                logging.debug("Removing unrecognized knowledge_map topic '%s'" % slug)
            elif not topictree_node.get("children"):
                logging.debug("Removing knowledge_map topic '%s' with no children." % slug)
            elif not "Exercise" in topictree_node.get("contains"):
                logging.debug("Removing knowledge_map topic '%s' with no exercises." % slug)
            else:
                continue

            del knowledge_map["topics"][slug]
            nodecache_node["in_knowledge_map"] = False
            topictree_node["in_knowledge_map"] = False

    scrub_knowledge_map(knowledge_map, node_cache)


    def recurse_nodes_to_extract_knowledge_map(node, node_cache):
        """
        Internal function for recursing the topic tree and building the knowledge map.
        Requires rebranding of metadata done by recurse_nodes function.
        """
        assert node["kind"] == "Topic"

        if node.get("in_knowledge_map", None):
            if node["slug"] not in knowledge_map["topics"]:
                logging.debug("Not in knowledge map: %s" % node["slug"])
                node["in_knowledge_map"] = False
                node_cache["Topic"][node["slug"]]["in_knowledge_map"] = False

            knowledge_topics[node["slug"]] = topic_tools.get_all_leaves(node, leaf_type="Exercise")

            if not knowledge_topics[node["slug"]]:
                sys.stderr.write("Removing topic from topic tree: no exercises. %s" % node["slug"])
                del knowledge_topics[node["slug"]]
                del knowledge_map["topics"][node["slug"]]
                node["in_knowledge_map"] = False
                node_cache["Topic"][node["slug"]]["in_knowledge_map"] = False
        else:
            if node["slug"] in knowledge_map["topics"]:
                sys.stderr.write("Removing topic from topic tree; does not belong. '%s'" % node["slug"])
                logging.debug("Removing from knowledge map: %s" % node["slug"])
                del knowledge_map["topics"][node["slug"]]

        for child in [n for n in node.get("children", []) if n["kind"] == "Topic"]:
            recurse_nodes_to_extract_knowledge_map(child, node_cache)
    recurse_nodes_to_extract_knowledge_map(topictree, node_cache)
    
    # Download icons
    def download_kmap_icons(knowledge_map):
        for key, value in knowledge_map["topics"].items():
            # Note: id here is retrieved from knowledge_map, so we're OK
            #   that we blew away ID in the topic tree earlier.
            if "icon_url" not in value:
                logging.debug("No icon URL for %s" % key)

            value["icon_url"] = iconfilepath + value["id"] + iconextension
            knowledge_map["topics"][key] = value

            out_path = data_path + "../" + value["icon_url"]
            if os.path.exists(out_path) and not force_icons:
                continue

            icon_khan_url = "http://www.khanacademy.org" + value["icon_url"]
            sys.stdout.write("Downloading icon %s from %s..." % (value["id"], icon_khan_url))
            sys.stdout.flush()
            try:
                icon = requests.get(icon_khan_url)
            except Exception as e:
                sys.stdout.write("\n")  # complete the "downloading" output
                sys.stderr.write("Failed to download %-80s: %s\n" % (icon_khan_url, e))
                continue
            if icon.status_code == 200:
                iconfile = file(data_path + "../" + value["icon_url"], "w")
                iconfile.write(icon.content)
            else:
                sys.stdout.write(" [NOT FOUND]")
                value["icon_url"] = iconfilepath + defaulticon + iconextension
            sys.stdout.write(" done.\n")  # complete the "downloading" output
    download_kmap_icons(knowledge_map)


    # Clean the knowledge map
    def clean_orphaned_polylines(knowledge_map):
        """
        We remove some topics (without leaves); need to remove polylines associated with these topics.
        """
        all_topic_points = [(km["x"],km["y"]) for km in knowledge_map["topics"].values()]

        polylines_to_delete = []
        for li, polyline in enumerate(knowledge_map["polylines"]):
            if any(["x" for pt in polyline["path"] if (pt["x"], pt["y"]) not in all_topic_points]):
                polylines_to_delete.append(li)

        logging.debug("Removing %s of %s polylines" % (len(polylines_to_delete), len(knowledge_map["polylines"])))
        for i in reversed(polylines_to_delete):
            del knowledge_map["polylines"][i]

        return knowledge_map
    clean_orphaned_polylines(knowledge_map)

    # Dump the knowledge map
    with open(os.path.join(data_path, topic_tools.map_layout_file), "w") as fp:
        fp.write(json.dumps(knowledge_map, indent=2))

    # Rewrite topicdata, obliterating the old (to remove cruft)
    topicdata_dir = os.path.join(data_path, "topicdata")
    if os.path.exists(topicdata_dir):
        shutil.rmtree(topicdata_dir)
        os.mkdir(topicdata_dir)
    for key, value in knowledge_topics.items():
        with open(os.path.join(topicdata_dir, "%s.json" % key), "w") as fp:
            fp.write(json.dumps(value, indent=2))

    return knowledge_map, knowledge_topics


def generate_node_cache(topictree=None, output_dir=settings.DATA_PATH):
    """
    Given the KA Lite topic tree, generate a dictionary of all Topic, Exercise, and Video nodes.
    """

    if not topictree:
        topictree = topic_tools.get_topic_tree(force=True)
    node_cache = {}


    def recurse_nodes(node, path="/"):
        # Add the node to the node cache
        kind = node["kind"]
        node_cache[kind] = node_cache.get(kind, {})
        
        if node["slug"] in node_cache[kind]:
            # Existing node, so append the path to the set of paths
            assert kind in topic_tools.multipath_kinds, "Make sure we expect to see multiple nodes map to the same slug (%s unexpected)" % kind

            # Before adding, let's validate some basic properties of the 
            #   stored node and the new node:
            # 1. Compare the keys, and make sure that they overlap 
            #      (except the stored node will not have 'path', but instead 'paths')
            # 2. For string args, check that values are the same
            #      (most/all args are strings, and ... I feel we're already being darn
            #      careful here.  So, I think it's enough.
            node_shared_keys = set(node.keys()) - set(["path"])
            stored_shared_keys = set(node_cache[kind][node["slug"]]) - set(["paths"])
            unshared_keys = node_shared_keys.symmetric_difference(stored_shared_keys)
            shared_keys = node_shared_keys.intersection(stored_shared_keys)
            assert not unshared_keys, "Node and stored node should have all the same keys."
            for key in shared_keys:
                # A cursory check on values, for strings only (avoid unsafe types)
                if isinstance(node[key], basestring):
                    assert node[key] == node_cache[kind][node["slug"]][key]

            # We already added this node, it's just found at multiple paths.
            #   So, save the new path
            node_cache[kind][node["slug"]]["paths"].append(node["path"])

        else:
            # New node, so copy off, massage, and store.
            node_copy = copy.copy(node)
            if "children" in node_copy:
                del node_copy["children"]
            if kind in topic_tools.multipath_kinds:
                # If multiple paths can map to a single slug, need to store all paths.
                node_copy["paths"] = [node_copy["path"]]
                del node_copy["path"]
            node_cache[kind][node["slug"]] = node_copy

        # Do the recursion
        for child in node.get("children", []):
            assert "path" in node and "paths" not in node, "This code can't handle nodes with multiple paths; it just generates them!"
            recurse_nodes(child, node["path"])


    recurse_nodes(topictree)

    with open(os.path.join(output_dir, topic_tools.node_cache_file), "w") as fp:
        fp.write(json.dumps(node_cache, indent=2))

    return node_cache


def create_youtube_id_to_slug_map(node_cache=None, data_path=settings.PROJECT_PATH + "/static/data/"):
    """
    Go through all videos, and make a map of youtube_id to slug, for fast look-up later
    """

    if not node_cache:
        node_cache = topic_tools.get_node_cache(force=True)

    map_file = os.path.join(data_path, topic_tools.video_remap_file)
    id2slug_map = dict()

    # Make a map from youtube ID to video slug
    for v in node_cache['Video'].values():
        assert v["youtube_id"] not in id2slug_map, "Make sure there's a 1-to-1 mapping between youtube_id and slug"
        id2slug_map[v['youtube_id']] = v['slug']

    # Save the map!
    with open(map_file, "w") as fp:
        fp.write(json.dumps(id2slug_map, indent=2))


def validate_data(topictree, node_cache, knowledge_map):

    # Validate related videos
    for exercise in node_cache['Exercise'].values():
        for vid in exercise.get("related_video_readable_ids", []):
            if not vid in node_cache["Video"]:
                sys.stderr.write("Could not find related video %s in node_cache (from exercise %s)\n" % (vid, exercise["slug"]))

    # Validate related exercises
    for video in node_cache["Video"].values():
        ex = video["related_exercise"]
        if ex and not ex["slug"] in node_cache["Exercise"]:
            sys.stderr.write("Could not find related exercise %s in node_cache (from video %s)\n" % (ex["slug"], video["slug"]))
            
    # Validate all topics have leaves
    for topic in node_cache["Topic"].values():
        if not topic_tools.get_topic_by_path(topic["path"], root_node=topictree).get("children"):
            sys.stderr.write("Could not find any children for topic %s\n" % (topic["path"]))

    # Validate all topics in knowledge map are in the node cache
    for slug in knowledge_map["topics"]:
        if slug not in node_cache["Topic"]:
            sys.stderr.write("Unknown topic in knowledge map: %s\n" % slug)

        topicdata_path = os.path.join(settings.PROJECT_PATH + "/static/data/", "topicdata", "%s.json" % slug)
        if not os.path.exists(topicdata_path):
            sys.stderr.write("Could not find topic data in topicdata directory: '%s'\n" % slug)

    # Validate all topics in node-cache are in (or out) of knowledge map, as requested.
    for topic in node_cache["Topic"].values():
        if topic["in_knowledge_map"] and not topic["slug"] in knowledge_map["topics"]:
            sys.stderr.write("Topic '%-40s' not in knowledge map, but node_cache says it should be.\n" % topic["slug"])

        elif not topic["in_knowledge_map"] and topic["slug"] in knowledge_map["topics"]:
            sys.stderr.write("Topic '%-40s' in knowledge map, but node_cache says it shouldn't be.\n" % topic["slug"])

        elif topic["in_knowledge_map"] and not topic_tools.get_topic_by_path(topic["path"], root_node=topictree).get("children"):
            sys.stderr.write("Topic '%-40s' in knowledge map, but has no children.\n" % topic["slug"])

        elif topic["in_knowledge_map"] and not topic_tools.get_all_leaves(topic_tools.get_topic_by_path(topic["path"], root_node=topictree), leaf_type="Exercise"):
            sys.stderr.write("Topic '%40s' in knowledge map, but has no exercises.\n" % topic["slug"])

class Command(BaseCommand):
    help = """**WARNING** not intended for use outside of the FLE; use at your own risk!
    Update the topic tree caches from Khan Academy.
    Options:
        [no args] - download from Khan Academy and refresh all files
        id2slug - regenerate the id2slug map file.
    """

    option_list = BaseCommand.option_list + (
        # Basic options
        make_option('-i', '--force-icons',
            action='store_true',
            dest='force_icons',
            default=False,
            help='Force the download of each icon'),
        make_option('-k', '--keep-new-exercises',
            action='store_true',
            dest='keep_new_exercises',
            default=False,
            help="Keep data on new exercises (if not specified, these are stripped out, as we don't have code to download/use them)"),
    )

    def handle(self, *args, **options):
        if len(args) != 0:
            raise CommandError("Unknown argument list: %s" % args)

        # TODO(bcipolli)
        # Make remove_unknown_exercises and force_icons into command-line arguments
        topictree = rebuild_topictree(remove_unknown_exercises=not options["keep_new_exercises"])
        node_cache = generate_node_cache(topictree)
        create_youtube_id_to_slug_map(node_cache)

        knowledge_map, _ = rebuild_knowledge_map(topictree, node_cache, force_icons=options["force_icons"])

        validate_data(topictree, node_cache, knowledge_map)

        sys.stdout.write("Downloaded topictree data for %d topics, %d videos, %d exercises\n" % (
            len(node_cache["Topic"]),
            len(node_cache["Video"]),
            len(node_cache["Exercise"]),
        ))