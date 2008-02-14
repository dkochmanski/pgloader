# Author: Dimitri Fontaine <dimitri@dalibo.com>
#
# pgloader database connection handling
# COPY dichotomy on error

import os, sys, os.path, time, codecs, logging
from cStringIO import StringIO

from options import DRY_RUN, PEDANTIC, CLIENT_MIN_MESSAGES
from options import TRUNCATE, VACUUM
from options import INPUT_ENCODING, PG_CLIENT_ENCODING, DATESTYLE
from options import COPY_SEP, FIELD_SEP, CLOB_SEP, NULL, EMPTY_STRING

from tools   import PGLoader_Error
from logger  import log

try:
    import psycopg2.psycopg1 as psycopg
except ImportError:
    log.info('No psycopg2 module found, trying psycopg1')
    import psycopg

class db:
    """ a db connexion and utility class """
    def __init__(self,
                 host, port, base, user, passwd,
                 client_encoding = PG_CLIENT_ENCODING,
                 copy_every = 10000, commit_every = 1000, connect = True):
        """ Connects to the specified database """
        self.log     = log
        self.dbconn  = None
        self.dsn     = "host=%s port=%d user=%s dbname=%s password=%s" \
                       % (host, port, user, base, passwd)
        self.connect = "-h %s -p %s -U %s" % (host, port, user)
        self.base    = base

        # those parameters can be overwritten after db init
        # here's their default values
        self.copy_sep        = COPY_SEP
        self.copy_every      = copy_every
        self.commit_every    = commit_every
        self.client_encoding = client_encoding
        self.datestyle       = DATESTYLE
        self.null            = NULL
        self.empty_string    = EMPTY_STRING
        self.lc_messages     = None

        if connect:
            self.reset()

    def __del__(self):
        """ db object destructor, we have to close the db connection """
        if self.dbconn is None:
            return

        if self.running_commands > 0:
            self.dbconn.commit()
            self.commits       += 1
            self.commited_rows += self.running_commands

        if self.dbconn is not None:
            self.dbconn.close()

    def set_encoding(self):
        """ set connection encoding to self.client_encoding """
        # debug only cause reconnecting happens on every
        # configured section
        self.log.debug('Setting client encoding to %s', self.client_encoding)
        
        sql = 'set session client_encoding to %s'
        cursor = self.dbconn.cursor()
        try:
            cursor.execute(sql, [self.client_encoding])
        except psycopg.ProgrammingError, e:
            raise PGLoader_Error, e
        cursor.close()

    def set_datestyle(self):
        """ set session datestyle to self.datestyle """

        if self.datestyle is None:
            return

        # debug only cause reconnecting happens on every
        # configured section
        self.log.debug('Setting datestyle to %s', self.datestyle)
        
        sql = 'set session datestyle to %s'
        cursor = self.dbconn.cursor()
        cursor.execute(sql, [self.datestyle])
        cursor.close()

    def set_lc_messages(self):
        """ set lc_messages to self.lc_messages """
        if self.lc_messages is None:
            return
        
        # debug only cause reconnecting happens on every
        # configured section
        self.log.debug('Setting lc_messages to %s', self.lc_messages)
        
        sql = 'set session lc_messages to %s'
        cursor = self.dbconn.cursor()
        try:
            cursor.execute(sql, [self.lc_messages])
        except psycopg.ProgrammingError, e:
            raise PGLoader_Error, e
        cursor.close()

    def reset(self):
        """ reset internal counters and open a new database connection """
        self.buffer            = None
        self.copy              = None # flag set to True when copy is called
        self.errors            = 0
        self.commits           = 0
        self.commited_rows     = 0
        self.running_commands  = 0
        self.last_commit_time  = time.time()
        self.first_commit_time = self.last_commit_time
        self.partial_coldef    = None

        if self.dbconn is not None:
            self.log.debug('Debug: closing current connection')
            self.dbconn.close()

        self.log.debug('Debug: connecting to dns %s', self.dsn)

        self.dbconn = psycopg.connect(self.dsn)
        self.set_encoding()
        self.set_datestyle()
        self.set_lc_messages()

    def print_stats(self):
        """ output some stats about recent activity """
        d = time.time() - self.first_commit_time
        u = self.commited_rows
        c = self.commits
        self.log.info(" %d updates in %d commits took %5.3f seconds", u, c, d)

        if self.errors > 0:
            self.log.error("%d database errors occured", self.errors)
            if self.copy and not VACUUM:
                self.log.info("Please VACUUM your database to recover space")
        else:
            if u > 0:
                self.log.info("No database error occured")
        return

    def is_null(self, value):
        """ return true if value is null, per configuration """
        return value == self.null

    def is_empty(self, value):
        """ return true if value is empty, per configuration """
        return value == self.empty_string

    def truncate(self, table):
        """ issue an SQL TRUNCATE TABLE on given table """
        if DRY_RUN:
            self.log.info("Won't truncate tables on dry-run mode")
            return
        
        sql = "TRUNCATE TABLE %s;" % table

        self.log.info('%s' % sql)
        
        try:
            cursor = self.dbconn.cursor()
            cursor.execute(sql)
            self.dbconn.commit()
        except Exception, error:
            self.log.error(error)
            raise PGLoader_Error, "Couldn't TRUNCATE table %s" % table
    
    def vacuum(self, table):
        """ issue VACUUM ANALYZE table """
        if DRY_RUN:
            self.log.info('no vacuum in dry-run mode')
            return -1

        sql = "VACUUM ANALYZE %s;" % table

        self.log.info('%s' % sql)
        
        try:
            cursor = self.dbconn.cursor()
            cursor.execute(sql)
            self.dbconn.commit()
        except Exception, error:
            self.log.error(error)
            raise PGLoader_Error, "Couldn't VACUUM table %s" % table

    def disable_triggers(self, table):
        """ issue ALTER TABLE table DISABLE TRIGGER ALL """
        if DRY_RUN:
            self.log.info("Won't disable triggers on dry-run mode")
            return
        
        sql = "ALTER TABLE %s DISABLE TRIGGER ALL;" % table

        self.log.info('%s' % sql)
        
        try:
            cursor = self.dbconn.cursor()
            cursor.execute(sql)
            self.dbconn.commit()
        except Exception, error:
            self.log.error(error)
            raise PGLoader_Error, "Couldn't DISABLE TRIGGERS on table %s" % table

    def enable_triggers(self, table):
        """ issue ALTER TABLE table ENABLE TRIGGER ALL """
        if DRY_RUN:
            self.log.info("Won't enable triggers on dry-run mode")
            return
        
        sql = "ALTER TABLE %s ENABLE TRIGGER ALL;" % table

        self.log.info('%s' % sql)
        
        try:
            cursor = self.dbconn.cursor()
            cursor.execute(sql)
            self.dbconn.commit()
        except Exception, error:
            self.log.error(error)
            raise PGLoader_Error, "Couldn't ENABLE TRIGGERS on table %s" % table

    
    def insert_blob(self, table, index, rowids,
                    blob_cname, data, btype,
                    input_line, reject):
        """ insert the given blob content into postgresql table

        return True on success, False on error
        """
        ok  = True
        sql = ""

        if btype == 'ifx_clob':
            data = data.replace("'", "\\'")
            sql = "UPDATE %s SET %s = %%s WHERE " % (table, blob_cname)

        elif btype == 'ifx_blob':
            data = data.tostring()
            sql = "UPDATE %s SET %s = %%s::bytea WHERE " % (table, blob_cname)

        values = [data]
        
        ##
        # Add a WHERE clause for each index
        first = True
        for name, col in index:
            if not first: sql  += " AND "
            else:         first = False
            
            sql += "%s = %%s" % name
            values.append(rowids[name])
        sql += ";"

        self.log.debug('%s' % sql)

        try:
            cursor = self.dbconn.cursor()
            cursor.execute(sql, values)

            # if execute raise an exception, don't count it as a
            # running command (waiting a commit)
            self.running_commands += 1

            if VERBOSE:
                str_rowids = ""
                for i,v in rowids.items():
                    if str_rowids != "": str_rowids += ", "
                    str_rowids += "%s:%s" % (i, v)
                self.log.debug('%s %s %s %6do', 
                          table, str_rowids, blob_cname, len(data))

            if self.running_commands == self.commit_every:
                now = time.time()
                self.dbconn.commit()
                
                self.commits += 1
                duration      = now - self.last_commit_time
                self.last_commit_time = now

                self.log.info("commit %d: %d updates in %5.3fs",
                         self.commits, self.running_commands, duration)

                self.commited_rows   += self.running_commands
                self.running_commands = 1

        except KeyboardInterrupt, error:
            # C-c was pressed, please stop processing
            self.dbconn.commit()
            raise PGLoader_Error, "Aborting on user demand (Interrupt)"

        except Exception, e:
            self.dbconn.commit()
            # don't use self.commited_rows here, it's only updated
            # after a commit
            self.log.error("update %d rejected: commiting (read log file %s)",
                      self.commits * self.commit_every + self.running_commands,
                      reject.reject_log)

            reject.log(str(e), input_line)
            self.errors += 1
            ok = False

        return ok

    def save_copy_buffer(self, tablename):
        """ save copy buffer to a temporary file for further inspection """
        import tempfile
        (f, n) = tempfile.mkstemp(prefix='%s.' % tablename,
                                  suffix='.pgloader', dir='/tmp')
        os.write(f, self.buffer.getvalue())
        os.close(f)
        self.log.warning("COPY data buffer saved in %s" % n)
        return n

    def copy_from(self, table, columnlist, columns, input_line,
                  reject, EOF = False):
        """ Generate some COPY SQL for PostgreSQL """
        ok = True
        if not self.copy: self.copy = True

        ##
        # build the table colomns specs from parameters
        # ie. we always issue COPY table (col1, col2, ..., coln) commands
        tablename = table
        table     = "%s (%s) " % (table, ", ".join(columnlist))

        if EOF or self.running_commands == self.copy_every \
               and self.buffer is not None:
            # time to copy data to PostgreSQL table

            if self.buffer is None:
                self.log.warning("no data to COPY")
                return False
            
            if CLIENT_MIN_MESSAGES <= logging.DEBUG:
                self.save_copy_buffer(tablename)

            self.buffer.seek(0)
            now = time.time()
                
            try:
                cursor = self.dbconn.cursor()
                r = cursor.copy_from(self.buffer, table, self.copy_sep)
                self.dbconn.commit()

                self.commits         += 1
                duration              = now - self.last_commit_time
                self.last_commit_time = now

                self.log.info("COPY %d: %d rows copied in %5.3fs",
                         self.commits, self.running_commands, duration)

                # prepare next run
                self.buffer.close()
                self.buffer = None
                self.commited_rows   += self.running_commands
                self.running_commands = 0

            except (psycopg.ProgrammingError,
                    psycopg.DatabaseError), error:
                # rollback current transaction
                self.dbconn.rollback()

                self.log.warning('COPY error, trying to find on which line')
                if CLIENT_MIN_MESSAGES > logging.DEBUG:
                    # in DEBUG mode, copy buffer has already been saved
                    # to file
                    self.save_copy_buffer(tablename)

                # copy recovery process
                now = time.time()
                c, ok, ko = self.copy_from_buff(table, self.buffer,
                                                self.running_commands, reject)

                duration              = now - self.last_commit_time
                self.commits         += c
                self.last_commit_time = now
                self.commited_rows   += ok
                self.errors          += ko

                self.log.warning('COPY error recovery done (%d/%d) in %5.3fs',
                            ko, ok, duration)

                # commit this transaction
                self.dbconn.commit()

                # recovery process has closed the buffer
                self.buffer = None
                self.running_commands = 0

        # prepare next run
        if self.buffer is None:
            self.buffer = StringIO()

        self.prepare_copy_data(columns, input_line, reject)
        self.running_commands += 1
        return ok

    def copy_from_buff(self, table, buff, count, reject):
        """ If copy returned an error, try to detect wrong input line(s) """

        if count == 1:
            reject.log('COPY error on this line', buff.getvalue())
            buff.close()
            self.log.debug('found one more line in error')

            # returns commits, ok, ko
            return 0, 0, 1
        
        ##
        # Dichotomy
        # we cut the buffer into two buffers, try to copy from them
        a = StringIO()
        b = StringIO()
        n = 0
        m = count / 2

        # return values, copied lines and errors
        commits = ok = ko = 0 

        buff.seek(0)
        for line in buff.readlines():
            if n < m:
                a.write(line)
            else:
                b.write(line)
            n += 1

        # we don't need no more orgininal buff
        buff.close()

        self.log.debug('Trying to find errors, dividing %d lines in %d and %d',
                  count, m, n-m)

        # now we have two buffers to copy to PostgreSQL database
        cursor = self.dbconn.cursor()
        for (x, xcount) in [(a, m), (b, n-m)]:
            try:
                x.seek(0)
                cursor.copy_from(x, table, self.copy_sep)
                self.dbconn.commit()
                x.close()

                self.log.debug("COPY ERROR handling progress: %d rows copied",
                          xcount)

                x.close()
                commits += 1
                ok += xcount

            except Exception, error:
                self.dbconn.commit()
                
                # if a is only one line long, reject this line
                if xcount == 1:
                    ko += 1
                    reject.log('COPY error: %s' % error, x.getvalue())
                    self.log.debug('Notice: found one more line in error')
                    self.log.debug(x.getvalue())

                else:
                    _c, _o, _k = self.copy_from_buff(table, x, xcount, reject)
                    commits += _c
                    ok += _o
                    ko += _k

        return commits, ok, ko


    def prepare_copy_data(self, columns, input_line, reject):
        """ add a data line to copy buffer """
        if columns is not None:
            first_col = True

            for c in columns:
                # default text format COPY delimiter
                if not first_col: self.buffer.write(self.copy_sep)
                else:             first_col = False

                if self.is_null(c):
                    # null column value: \N
                    self.buffer.write('\N')

                elif self.is_empty(c):
                    # empty string has been read
                    self.log.debug("empty string read: '%s'" % c)
                    self.buffer.write('')

                else:
                    # for a list of chars to replace, please have a look to
                    # http://www.postgresql.org/docs/8.1/static/sql-copy.html
                    if INPUT_ENCODING is not None:
                        try:
                            c = c.encode(INPUT_ENCODING)
                        except UnicodeDecodeError, e:
                            reject.log(['Codec error', str(e)], input_line)

                    # in _split_line we remove delimiter escaping
                    # in order for backslash escaping not to de-escape it
                    # we then have to escape delimiters explicitely now
                    for orig, escaped in [('\\', '\\\\'),
                                          (self.copy_sep,
                                           '\\%s' % self.copy_sep),
                                          ('\b', '\\b'),
                                          ('\f', '\\f'),
                                          ('\n', '\\n'),
                                          ('\r', '\\r'),
                                          ('\t', '\\t'),
                                          ('\v', '\\v')]:
                        c = c.replace(orig, escaped)

                    self.buffer.write(c)

            # end of row, \n
            self.buffer.write('\n')