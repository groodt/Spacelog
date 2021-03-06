from __future__ import with_statement
import os
import sys
import re
import redis
import xappy
try:
    import json
except ImportError:
    import simplejson as json
from django.utils.html import strip_tags

from backend.parser import TranscriptParser, MetaParser
from backend.api import Act, KeyScene, Character, Glossary, LogLine
from backend.util import seconds_to_timestamp

search_db = xappy.IndexerConnection(
    os.path.join(os.path.dirname(__file__), '..', 'xappydb'),
)

def mission_time_to_timestamp(mission_time):
    """Takes a mission time string (XX:XX:XX:XX) and converts it to a number of seconds"""
    d,h,m,s = map(int, mission_time.split(':'))
    timestamp = d*86400 + h*3600 + m*60 + s
    
    if mission_time[0] == "-":
        return timestamp * -1
    else:
        return timestamp

class TranscriptIndexer(object):
    """
    Parses a file and indexes it.
    """

    LINES_PER_PAGE = 50

    def __init__(self, redis_conn, mission_name, transcript_name, parser):
        self.redis_conn = redis_conn
        self.mission_name = mission_name
        self.transcript_name = transcript_name
        self.parser = parser

        search_db.add_field_action(
            "mission",
            xappy.FieldActions.INDEX_EXACT,
            # search_by_default=False,
            # allow_field_specific=False,
        )
        search_db.add_field_action(
            "transcript",
            xappy.FieldActions.INDEX_EXACT,
        )
        # don't think we need STORE_CONTENT actions any more
        search_db.add_field_action(
            "speaker",
            xappy.FieldActions.STORE_CONTENT,
        )
        # Can't use facetting unless Xapian supports it
        # can't be bothered to check this (xappy._checkxapian.missing_features['facets']==1)
        #
        # search_db.add_field_action(
        #     "speaker",
        #     xappy.FieldActions.FACET,
        #     type='string',
        # )
        search_db.add_field_action(
            "speaker",
            xappy.FieldActions.INDEX_FREETEXT,
            weight=1,
            language='en',
            search_by_default=True,
            allow_field_specific=True,
        )
        search_db.add_field_action(
            "text",
            xappy.FieldActions.STORE_CONTENT,
        )
        search_db.add_field_action(
            "text",
            xappy.FieldActions.INDEX_FREETEXT,
            weight=1,
            language='en',
            search_by_default=True,
            allow_field_specific=False,
            spell=True,
        )
        search_db.add_field_action(
            "weight",
            xappy.FieldActions.SORTABLE,
            type='float',
        )
        search_db.add_field_action(
            "speaker_identifier",
            xappy.FieldActions.STORE_CONTENT,
        )
        # Add names as synonyms for speaker identifiers
        characters = Character.Query(self.redis_conn, self.mission_name).items()
        self.characters = {}
        for character in characters:
            self.characters[character.identifier] = character
        #     for name in [character.name, character.short_name]:
        #         for bit in name.split():
        #             search_db.add_synonym(bit, character.identifier)
        #             search_db.add_synonym(bit, character.identifier, field='speaker')

    def add_to_search_index(self, mission, id, lines, weight, timestamp):
        """
        Take some text and a set of speakers (also text) and add a document
        to the search index, with the id stuffed in the document data.
        """
        doc = xappy.UnprocessedDocument()
        doc.fields.append(xappy.Field("mission", mission))
        doc.fields.append(xappy.Field("weight", weight))
        doc.fields.append(xappy.Field("transcript", self.transcript_name))
        for line in lines:
            text = re.sub(
                r"\[\w+:([^]]+)\|([^]]+)\]",
                lambda m: m.group(2),
                line['text'],
            )
            text = re.sub(
                r"\[\w+:([^]]+)\]",
                lambda m: m.group(1),
                text,
            )
            # also strip tags from text, because they're lame lame lame
            text = strip_tags(text)
            doc.fields.append(xappy.Field("text", text))
            # grab the character to get some more text to index under speaker
            ch = self.characters.get(line['speaker'], None)
            if ch:
                ch2 = ch.current_shift(timestamp)
                doc.fields.append(xappy.Field("speaker_identifier", ch2.identifier))
                doc.fields.append(xappy.Field("speaker", ch2.short_name))
                doc.fields.append(xappy.Field("speaker", ch.short_name))
            else:
                doc.fields.append(xappy.Field("speaker_identifier", line['speaker']))
                doc.fields.append(xappy.Field("speaker", line['speaker']))
        doc.id = id
        try:
            search_db.add(search_db.process(doc))
        except xappy.errors.IndexerError:
            print "umm, error"
            print id, lines
            raise

    def index(self):
        current_labels = {}
        current_transcript_page = None
        current_page = 1
        current_page_lines = 0
        last_act = None
        previous_log_line_id = None
        previous_timestamp = None
        launch_time = int(self.redis_conn.hget("mission:%s" % self.mission_name, "utc_launch_time"))
        acts = list(Act.Query(self.redis_conn, self.mission_name))
        key_scenes = list(KeyScene.Query(self.redis_conn, self.mission_name))
        glossary_items = dict([
            (item.identifier.lower(), item) for item in
            Glossary.Query(self.redis_conn, self.mission_name)
        ])
        for chunk in self.parser.get_chunks():
            timestamp = chunk['timestamp']
            log_line_id = "%s:%i" % (self.transcript_name, timestamp)
            if timestamp <= previous_timestamp:
                raise Exception, "%s should be after %s" % (seconds_to_timestamp(timestamp), seconds_to_timestamp(previous_timestamp))
            # See if there's transcript page info, and update it if so
            if chunk['meta'].get('_page', 0):
                current_transcript_page = int(chunk["meta"]['_page'])
            if current_transcript_page:
                self.redis_conn.set("log_line:%s:page" % log_line_id, current_transcript_page)
            # Look up the act
            for act in acts:
                if act.includes(timestamp):
                    break
            else:
                print "Error: No act for timestamp %s" % seconds_to_timestamp(timestamp)
                continue
            # If we've filled up the current page, go to a new one
            if current_page_lines >= self.LINES_PER_PAGE or (last_act is not None and last_act != act):
                current_page += 1
                current_page_lines = 0
            last_act = act
            # First, create a record with some useful information
            info_key = "log_line:%s:info" % log_line_id
            info_record = {
                "offset": chunk['offset'],
                "page": current_page,
                "act": act.number,
                "utc_time": launch_time + timestamp,
            }
            if current_transcript_page:
                info_record["transcript_page"] = current_transcript_page

            self.redis_conn.hmset(
                info_key,
                info_record,
            )
            # Look up the key scene
            for key_scene in key_scenes:
                if key_scene.includes(timestamp):
                    self.redis_conn.hset(info_key, 'key_scene', key_scene.number)
                    break
            # Create the doubly-linked list structure
            if previous_log_line_id:
                self.redis_conn.hset(
                    info_key,
                    "previous",
                    previous_log_line_id,
                )
                self.redis_conn.hset(
                    "log_line:%s:info" % previous_log_line_id,
                    "next",
                    log_line_id,
                )
            previous_log_line_id = log_line_id
            previous_timestamp = timestamp
            # Also store the text
            text = ""
            for line in chunk['lines']:
                self.redis_conn.rpush(
                    "log_line:%s:lines" % log_line_id,
                    "%(speaker)s: %(text)s" % line,
                )
                text += "%s %s" % (line['speaker'], line['text'])
            # Store any images
            for i, image in enumerate(chunk['meta'].get("_images", [])):
                # Make the image id
                image_id = "%s:%s" % (log_line_id, i)
                # Push it onto the images list
                self.redis_conn.rpush(
                    "log_line:%s:images" % log_line_id,
                    image_id,
                )
                # Store the image data
                self.redis_conn.hmset(
                    "image:%s" % image_id,
                    image,
                )
            # Add that logline ID for the people involved
            speakers = set([ line['speaker'] for line in chunk['lines'] ])
            for speaker in speakers:
                self.redis_conn.sadd("speaker:%s" % speaker, log_line_id)
            # Add it to the index for this page
            self.redis_conn.rpush("page:%s:%i" % (self.transcript_name, current_page), log_line_id)
            # Add it into the transcript and everything sets
            self.redis_conn.zadd("log_lines:%s" % self.mission_name, log_line_id, chunk['timestamp'])
            self.redis_conn.zadd("transcript:%s" % self.transcript_name, log_line_id, chunk['timestamp'])
            # Read the new labels into current_labels
            has_labels = False
            if '_labels' in chunk['meta']:
                for label, endpoint in chunk['meta']['_labels'].items():
                    if endpoint is not None and label not in current_labels:
                        current_labels[label] = endpoint
                    elif label in current_labels:
                        current_labels[label] = max(
                            current_labels[label],
                            endpoint
                        )
                    elif endpoint is None:
                        self.redis_conn.sadd("label:%s" % label, log_line_id)
                        has_labels = True
            # Expire any old labels
            for label, endpoint in current_labels.items():
                if endpoint < chunk['timestamp']:
                    del current_labels[label]
            # Apply any surviving labels
            for label in current_labels:
                self.redis_conn.sadd("label:%s" % label, log_line_id)
                has_labels = True
            # And add this logline to search index
            if has_labels:
                print "weight = 3 for %s" % log_line_id
                weight = 3.0 # magic!
            else:
                weight = 1.0
            self.add_to_search_index(
                mission=self.mission_name,
                id=log_line_id,
                lines = chunk['lines'],
                weight=weight,
                timestamp=timestamp,
            )
            # For any mentioned glossary terms, add to them.
            for word in text.split():
                word = word.strip(",;-:'\"").lower()
                if word in glossary_items:
                    glossary_item = glossary_items[word]
                    self.redis_conn.hincrby(
                        "glossary:%s" % glossary_item.id,
                        "times_mentioned",
                        1,
                    )
            # Increment the number of log lines we've done
            current_page_lines += len(chunk['lines'])


class MetaIndexer(object):
    """
    Takes a mission folder and reads and indexes its meta information.
    """

    def __init__(self, redis_conn, mission_name, parser):
        self.redis_conn = redis_conn
        self.parser = parser
        self.mission_name = mission_name

    def index(self):
        meta = self.parser.get_meta()

        # Store mission info
        for subdomain in meta['subdomains']:
            self.redis_conn.set("subdomain:%s" % subdomain, meta['name'])
        del meta['subdomains']
        self.redis_conn.hmset(
            "mission:%s" % self.mission_name,
            {
                "utc_launch_time": meta['utc_launch_time'],
                "featured": meta.get('featured', False),
                "incomplete": meta.get('incomplete', False),
                "main_transcript": meta.get('main_transcript', None),
                "media_transcript": meta.get('media_transcript', None),
            }
        )
        copy = meta.get("copy", {})
        for key, value in copy.items():
            copy[key] = json.dumps(value)
        self.redis_conn.hmset(
            "mission:%s:copy" % self.mission_name,
            copy,
        )
        for homepage_quote in meta.get('homepage_quotes', []):
            self.redis_conn.sadd(
                "mission:%s:homepage_quotes" % self.mission_name,
                homepage_quote,
            )

        self.index_narrative_elements(meta)
        self.index_glossary(meta)
        self.index_characters(meta)
        self.index_special_searches(meta)
        self.index_errors(meta)

    def index_narrative_elements(self, meta):
        "Stores acts and key scenes in redis"
        for noun in ('act', 'key_scene'):
            for i, data in enumerate(meta.get('%ss' % noun, [])):
                key = "%s:%s:%i" % (noun, self.mission_name, i)
                self.redis_conn.rpush(
                    "%ss:%s" % (noun, self.mission_name),
                    "%s:%i" % (self.mission_name, i),
                )

                data['start'], data['end'] = map(mission_time_to_timestamp, data['range'])
                del data['range']

                self.redis_conn.hmset(key, data)
        # Link key scenes and acts
        for act in Act.Query(self.redis_conn, self.mission_name):
            for key_scene in KeyScene.Query(self.redis_conn, self.mission_name):
                if act.includes(key_scene.start):
                    self.redis_conn.rpush(
                        'act:%s:%s:key_scenes' % (self.mission_name, act.number),
                        '%s:%s' % (self.mission_name, key_scene.number),
                    )

    def index_characters(self, meta):
        "Stores character information in redis"
        for identifier in meta.get("character_ordering", []):
            self.redis_conn.rpush(
                "character-ordering:%s" % self.mission_name,
                identifier,
            )
        for identifier, data in meta.get('characters', {}).items():
            mission_key   = "characters:%s" % self.mission_name
            character_key = "%s:%s" % (mission_key, identifier)
            
            self.redis_conn.rpush(mission_key, identifier)
            self.redis_conn.rpush(
                '%s:%s' % (mission_key, data['role']),
                identifier
            )
            
            # Push stats as a list so it's in-order later
            if 'stats' in data:
                for stat in data['stats']:
                    self.redis_conn.rpush(
                        '%s:stats' % character_key, 
                        "%s:%s" % (stat['value'], stat['text'])
                    )
                del data['stats']
            
            # Store the shifts
            if 'shifts' in data:
                for shift_information in data['shifts']:
                    character_identifier = shift_information[0]
                    shift_start = shift_information[1]
                    
                    shift_start = mission_time_to_timestamp(shift_start)
                    shifts_key = '%s:shifts' % character_key
                    self.redis_conn.zadd(
                        shifts_key,
                        '%s:%s' % (shift_start, character_identifier),
                        shift_start
                    )
                del data['shifts']
            
            self.redis_conn.hmset(character_key, data)

    def index_glossary(self, meta):
        """
        Stores glossary information in redis.
        Terms from the mission's shared glossary file(s) will be overridden by terms
        from the mission's own _meta file.
        """
        
        glossary_terms = {}
        
        # Load any shared glossary files and add their contents
        # to glossary_terms
        shared_glossary_files       = meta.get('shared_glossaries', [])
        shared_glossary_files_path  = os.path.join(os.path.dirname( __file__ ), '..', 'missions', 'shared', 'glossary')
        
        for filename in shared_glossary_files:
            with open(os.path.join(shared_glossary_files_path, filename)) as fh:
                glossary_terms.update(json.load(fh))
        
        # Add the mission specific glossary terms
        glossary_terms.update(meta.get('glossary', {}))
        
        # Add all the glossary terms to redis
        for identifier, data in glossary_terms.items():
            term_key = "%s:%s" % (self.mission_name, identifier.lower())
            
            # Add the ID to the list for this mission
            self.redis_conn.rpush("glossary:%s" % self.mission_name, identifier)

            # Extract the links from the data
            links = data.get('links', [])
            if "links" in data:
                del data['links']
            
            data['abbr'] = identifier
            data['times_mentioned'] = 0
            
            if data.has_key('summary') and data.has_key('description'):
                data['extended_description'] = data['description']
                data['description'] = data['summary']
                del data['summary']
            else:
                data['description'] = data.get('summary') or data['description']
            
            # Store the main data in a hash
            self.redis_conn.hmset("glossary:%s" % term_key, data)

            # Store the links in a list
            for i, link in enumerate(links):
                link_id = "%s:%i" % (term_key, i)
                self.redis_conn.rpush("glossary:%s:links" % term_key, link_id)
                self.redis_conn.hmset(
                    "glossary-link:%s" % link_id,
                    link,
                )

    def index_special_searches(self, meta):
        "Indexes things that in no way sound like 'feaster legs'."
        for search, value in meta.get('special_searches', {}).items():
            self.redis_conn.set("special_search:%s:%s" % (self.mission_name, search), value)

    def index_errors(self, meta):
        "Indexes error page info"
        for key, info in meta.get('error_pages', {}).items():
            self.redis_conn.hmset(
                "error_page:%s:%s" % (self.mission_name, key),
                info,
            )


class MissionIndexer(object):
    """
    Takes a mission folder and indexes everything inside it.
    """

    def __init__(self, redis_conn, mission_name, folder_path):
        self.redis_conn = redis_conn
        self.folder_path = folder_path
        self.mission_name = mission_name

    def index(self):
        self.index_meta()
        self.index_transcripts()

    def index_transcripts(self):
        for filename in os.listdir(self.folder_path):
            if "." not in filename and filename[0] != "_" and filename[-1] != "~":
                print "Indexing %s..." % filename
                path = os.path.join(self.folder_path, filename)
                parser = TranscriptParser(path)
                indexer = TranscriptIndexer(self.redis_conn, self.mission_name, "%s/%s" % (self.mission_name, filename), parser)
                indexer.index()

    def index_meta(self):
        print "Indexing _meta..."
        path = os.path.join(self.folder_path, "_meta")
        parser = MetaParser(path)
        indexer = MetaIndexer(self.redis_conn, self.mission_name, parser)
        indexer.index()


if __name__ == "__main__":
    redis_conn = redis.Redis()
    redis_conn.flushdb()
    redis_conn.set("hold", "1")
    transcript_dir = os.path.join(os.path.dirname( __file__ ), '..', "missions")
    if len(sys.argv)>1:
        dirs = sys.argv[1:]
    else:
        dirs = os.listdir(transcript_dir)
    for filename in dirs:
        path = os.path.join(transcript_dir, filename)
        if filename[0] not in "_." and os.path.isdir(path) and os.path.exists(os.path.join(path, "transcripts", "_meta")):
            print "Mission: %s" % filename
            idx = MissionIndexer(redis_conn, filename, os.path.join(path, "transcripts")) 
            idx.index()
    search_db.flush()
    redis_conn.delete("hold")

