from app.utils import prepare_validation
from flask import Flask, request, Response, g
import os, time, logging, json
from SPARQLWrapper import SPARQLWrapper, JSON
import multiprocessing as mp

from app.query import Query
import app.colors as Colors
from app.config import Config
from app.utils import lookForException
from app.output.simpleOutput import SimpleOutput
from app.output.baseResult import BaseResult
from app.output.testOutput import TestOutput
from app.output.statsOutput import StatsOutput
from app.multiprocessing.functions import queue_output_to_table, mp_validate, mp_xjoin
from app.multiprocessing.runner import Runner
from app.multiprocessing.contactSource import contactSource
from app.reduction.ValidationResultTransmitter import ValidationResultTransmitter
from app.output.statsCalculation import StatsCalculation

app = Flask(__name__)
logging.getLogger('werkzeug').disabled = True

EXTERNAL_SPARQL_ENDPOINT: SPARQLWrapper = None
VALIDATION_RESULT_ENDPOINT = "http://localhost:5000/newValidationResult"

# Profiling Code
from pyinstrument import Profiler
global_request_count = 0

# This seems to load some pyparsing stuff and will speed up the execution of the first task by 1 second.
query = Query.prepare_query("PREFIX test1:<http://example.org/testGraph1#>\nSELECT DISTINCT ?x WHERE {\n?x a test1:classE.\n?x test1:has ?lit.\n}")
query.namespace_manager.namespaces()

# Building Multiprocessing Chain using Runners and Queries
# Validation --> \
#                 XJoin --> Output Generation
# Query      --> /

# Dataprocessing Queues --> 'EOF' is written by the runner class after function to execute finished
# val_queue: Queue with validation results
# transformed_query_queue: Query results in a joinable format
# query_queue: All results in original binding format
# out_queue: Joined results (literals/non-shape uris missing, and still need to collect bindings with similar id)
# 
# Queues to collect statistics: --> {"topic":...., "":....}
# stats_out_queue: one time statistics per run --> known number of statistics (also contains exception notifications in case a runner catches an exception)
# result_timing_out_queue: variable number of result timestamps per run --> close with 'EOF' by queue_output_to_table


VALIDATION_RUNNER = Runner(mp_validate)
val_queue, stats_out_queue = VALIDATION_RUNNER.get_out_queues()

CONTACT_SOURCE_RUNNER = Runner(contactSource, number_of_out_queues=2, runner_stats_out_queue=stats_out_queue)
transformed_query_queue, query_queue, _ = CONTACT_SOURCE_RUNNER.get_out_queues()

XJOIN_RUNNER = Runner(mp_xjoin, in_queues=[transformed_query_queue, val_queue], runner_stats_out_queue=stats_out_queue)
out_queue, _  = XJOIN_RUNNER.get_out_queues()

result_timing_out_queue = mp.Queue()

# Starting the processes of the runners
VALIDATION_RUNNER.start_process()
CONTACT_SOURCE_RUNNER.start_process()
XJOIN_RUNNER.start_process()

@app.route("/endpoint", methods=['GET', 'POST'])
def endpoint():
    '''
    This is just an proxy endpoint to log the communication between the backend and the external sparql endpoint.
    '''
    global EXTERNAL_SPARQL_ENDPOINT
    print(Colors.green(Colors.headline('SPARQL Endpoint Request')))
    # Preprocessing of the Query
    if request.method == 'POST':
        query = request.form['query']
    if request.method == 'GET':
        query = request.args['query']

    print("Received Query: ")
    print(Colors.grey(query))

    start = time.time()
    EXTERNAL_SPARQL_ENDPOINT.setQuery(query)
    result = EXTERNAL_SPARQL_ENDPOINT.query().convert()
    jsonResult = json.dumps(result)
    end = time.time()

    print("Got {} result bindings".format(len(result['results']['bindings'])))
    print("Execution took " + str((end - start)*1000) + ' ms')
    print(Colors.green(Colors.headline('')))

    return Response(jsonResult, mimetype='application/json')

@app.route("/newValidationResult", methods=['POST'])
def enqueueValidationResult():
    global val_queue
    new_val_result = {'instance': request.form['instance'], 
                        'validation': (request.form['shape'], request.form['validation_result'] == 'valid', request.form['reason'])}
    print("Received", new_val_result)
    val_queue.put(new_val_result)
    return 'Ok'

@app.route("/multiprocessing", methods=['POST'])
def run_multiprocessing():
    '''
    Required Arguments:
        - query
        - targetShape
        - external_endpoint
        - schemaDir
    See app/config.py for a full list of available arguments!
    '''
    global EXTERNAL_SPARQL_ENDPOINT
    EXTERNAL_SPARQL_ENDPOINT = None

    # Parse Config from POST Request and Config File
    config = Config.from_request_form(request.form)

    # Setup Stats Calculation
    statsCalc = StatsCalculation(test_identifier = config.test_identifier, approach_name = os.path.basename(config.config))
    statsCalc.globalCalculationStart()

    # Setup of the Validation Result Transmitting Strategie
    if config.transmission_strategy == 'endpoint':
        result_transmitter = ValidationResultTransmitter(validation_result_endpoint=VALIDATION_RESULT_ENDPOINT, first_val_time_queue=stats_out_queue, log_stats=(config.output_format == "stats"))
    elif config.transmission_strategy == 'queue':
        result_transmitter = ValidationResultTransmitter(output_queue=val_queue, first_val_time_queue=stats_out_queue, log_stats=(config.output_format == "stats"))
    else:
        result_transmitter = ValidationResultTransmitter(first_val_time_queue=stats_out_queue, log_stats=(config.output_format == "stats"))

    EXTERNAL_SPARQL_ENDPOINT = SPARQLWrapper(config.external_endpoint, returnFormat=JSON)
    os.makedirs(os.path.join(os.getcwd(), config.output_directory), exist_ok=True)

    # Parse query_string into a corresponding Query Object    
    query = Query.prepare_query(config.query)

    # The information we need depends on the output format:
    if config.output_format == "test":
        query_to_be_executed = query.as_valid_query()
    else:
        query_to_be_executed = query.as_result_query()

    statsCalc.taskCalculationStart()

    # 1.) Get the Data
    CONTACT_SOURCE_RUNNER.new_task(config.output_format == "stats", config.internal_endpoint if not config.send_initial_query_over_internal_endpoint else config.INTERNAL_SPARQL_ENDPOINT, query_to_be_executed, -1)
    VALIDATION_RUNNER.new_task(config.output_format == "stats", config, query, result_transmitter)

    # 2.) Join the Data
    XJOIN_RUNNER.new_task(config.output_format == "stats", config)

    # 3.) Result Collection: Order the Data and Restore missing vars (these one which could not find a join partner (literals etc.))
    try:
        if config.output_format == "stats":
            api_result = queue_output_to_table(out_queue, query_queue, result_timing_out_queue)
        else:
            api_result = queue_output_to_table(out_queue, query_queue)
    except:
        return "Stopped @ queue_output_to_table"


    # 4.) Output
    if config.output_format == "test":
        lookForException(stats_out_queue)
        api_output = TestOutput.fromJoinedResults(config.target_shape,api_result)
    elif config.output_format == "simple":
        lookForException(stats_out_queue)
        api_output = SimpleOutput.fromJoinedResults(api_result, query)
    elif config.output_format == "stats":
        TestOutput.fromJoinedResults(config.target_shape, api_result)
        statsCalc.globalCalculationFinished()

        output_directory = os.path.join(os.getcwd(), config.output_directory)
        matrix_file = os.path.join(output_directory, "matrix.csv")
        trace_file = os.path.join(output_directory, "trace.csv")
        stats_file = os.path.join(output_directory, "stats.csv")

        statsCalc.receive_and_write_trace(trace_file, result_timing_out_queue)
        statsCalc.receive_global_stats(stats_out_queue)
        api_output = statsCalc.write_matrix_and_stats_files(matrix_file, stats_file)

    return Response(api_output.to_json(config.target_shape), mimetype='application/json')


@app.route("/singleprocessing", methods=['POST'])
def run():
    '''
    ONLY COMPATIBLE WITH TRAVSHACL BACKEND!

    Required Arguments:
        - query
        - targetShape
        - external_endpoint
        - schemaDir
    See app/config.py for a full list of available arguments!
    '''
    # start_profiling()
    # Each run can be over a different Endpoint, so the endpoint needs to be recreated
    global EXTERNAL_SPARQL_ENDPOINT
    EXTERNAL_SPARQL_ENDPOINT = None
    result_transmitter = ValidationResultTransmitter()

    # Parse Config from POST Request and Config File
    config = Config.from_request_form(request.form)
    
    EXTERNAL_SPARQL_ENDPOINT = SPARQLWrapper(config.external_endpoint, returnFormat=JSON)
    os.makedirs(os.path.join(os.getcwd(), config.output_directory), exist_ok=True)

    # Parse query_string into a corresponding select_query
    query = Query.prepare_query(config.query)
    schema = prepare_validation(config, query, result_transmitter) # True means replace TargetShape Query
    
    # Run the evaluation of the SHACL constraints over the specified endpoint
    report = schema.validate(start_with_target_shape=True)
    
    # Retrieve the complete result for the initial query
    EXTERNAL_SPARQL_ENDPOINT.setQuery(query.as_result_query())
    results = EXTERNAL_SPARQL_ENDPOINT.query().convert()
    # stop_profiling()
    if config.output_format == "test":
        return Response(TestOutput(BaseResult.from_travshacl(report, query, results)).to_json(config.target_shape), mimetype='application/json')
    else:
        return Response(str(SimpleOutput(BaseResult.from_travshacl(report, query, results))))

@app.route("/", methods=['GET'])
def hello_world():
    return "Hello World"

@app.route("/stop", methods=['GET'])
def stop():
    VALIDATION_RUNNER.stop_process()
    CONTACT_SOURCE_RUNNER.stop_process()
    XJOIN_RUNNER.stop_process()
    time.sleep(0.1)
    VALIDATION_RUNNER.clear_queues()
    CONTACT_SOURCE_RUNNER.clear_queues()
    XJOIN_RUNNER.clear_queues()
    print("Clearing result timing queue")
    VALIDATION_RUNNER.clear_queue(result_timing_out_queue)
    out_queue.put('STOP')
    query_queue.put('STOP')
    time.sleep(1)
    VALIDATION_RUNNER.clear_queue(out_queue)
    VALIDATION_RUNNER.clear_queue(query_queue)
    return str(VALIDATION_RUNNER.process_is_alive()) + str(CONTACT_SOURCE_RUNNER.process_is_alive()) + str(XJOIN_RUNNER.process_is_alive())


@app.route("/start", methods=['GET'])
def start():
    VALIDATION_RUNNER.start_process()
    CONTACT_SOURCE_RUNNER.start_process()
    XJOIN_RUNNER.start_process()
    time.sleep(0.1)
    global result_timing_out_queue
    result_timing_out_queue = mp.Queue()
    return str(VALIDATION_RUNNER.process_is_alive()) + str(CONTACT_SOURCE_RUNNER.process_is_alive()) + str(XJOIN_RUNNER.process_is_alive())

def start_profiling():
    g.profiler = Profiler()
    g.profiler.start()
    print(Colors.magenta(Colors.headline('New Validation Task')))

def stop_profiling():
    global global_request_count
    g.profiler.stop()
    output_html = g.profiler.output_html()
    global_request_count = global_request_count + 1
    with open("timing/api_profil{}.html".format(global_request_count - 1),"w") as f:
        f.write(output_html)
