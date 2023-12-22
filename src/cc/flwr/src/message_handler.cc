#include "message_handler.h"

std::tuple<flwr::proto::ClientMessage, int>
_reconnect(flwr::proto::ServerMessage_ReconnectIns reconnect_msg) {
  // Determine the reason for sending Disconnect message
  flwr::proto::Reason reason = flwr::proto::Reason::ACK;
  int sleep_duration = 0;
  if (reconnect_msg.seconds() != 0) {
    reason = flwr::proto::Reason::RECONNECT;
    sleep_duration = reconnect_msg.seconds();
  }

  // Build Disconnect message
  flwr::proto::ClientMessage_DisconnectRes disconnect;
  disconnect.set_reason(reason);
  flwr::proto::ClientMessage cm;
  *cm.mutable_disconnect_res() = disconnect;

  return std::make_tuple(cm, sleep_duration);
}

flwr::proto::ClientMessage _get_parameters(flwr_local::Client *client) {
  flwr::proto::ClientMessage cm;
  *(cm.mutable_get_parameters_res()) =
      parameters_res_to_proto(client->get_parameters());
  return cm;
}

flwr::proto::ClientMessage _fit(flwr_local::Client *client,
                                flwr::proto::ServerMessage_FitIns fit_msg) {
  // Deserialize fit instruction
  flwr_local::FitIns fit_ins = fit_ins_from_proto(fit_msg);
  // Perform fit
  flwr_local::FitRes fit_res = client->fit(fit_ins);
  // Serialize fit result
  flwr::proto::ClientMessage cm;
  *cm.mutable_fit_res() = fit_res_to_proto(fit_res);
  return cm;
}

flwr::proto::ClientMessage
_evaluate(flwr_local::Client *client,
          flwr::proto::ServerMessage_EvaluateIns evaluate_msg) {
  // Deserialize evaluate instruction
  flwr_local::EvaluateIns evaluate_ins = evaluate_ins_from_proto(evaluate_msg);
  // Perform evaluation
  flwr_local::EvaluateRes evaluate_res = client->evaluate(evaluate_ins);
  // Serialize evaluate result
  flwr::proto::ClientMessage cm;
  *cm.mutable_evaluate_res() = evaluate_res_to_proto(evaluate_res);
  return cm;
}

bool validate_task_ins(const flwr::proto::TaskIns &task_ins,
                       const bool discard_reconnect_ins) {
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wdeprecated-declarations"
  return !(!task_ins.has_task() ||
           (!task_ins.task().has_legacy_server_message() &&
            !task_ins.task().has_sa()) ||
           (discard_reconnect_ins &&
            task_ins.task().legacy_server_message().has_reconnect_ins()));
#pragma GCC diagnostic pop
}

bool validate_task_res(const flwr::proto::TaskRes &task_res) {
  // Retrieve initialized fields in TaskRes
  return (task_res.task_id().empty() && task_res.group_id().empty() &&
          task_res.workload_id() == 0 && !task_res.task().has_producer() &&
          !task_res.task().has_producer() && !task_res.task().has_consumer() &&
          task_res.task().ancestry_size() == 0);
}

flwr::proto::TaskRes
configure_task_res(const flwr::proto::TaskRes &task_res,
                   const flwr::proto::TaskIns &ref_task_ins,
                   const flwr::proto::Node &producer) {
  flwr::proto::TaskRes result_task_res;

  // Setting scalar fields
  result_task_res.set_task_id(""); // This will be generated by the server
  result_task_res.set_group_id(ref_task_ins.group_id());
  result_task_res.set_workload_id(ref_task_ins.workload_id());

  // Merge the task from the input task_res
  *result_task_res.mutable_task() = task_res.task();

  // Construct and set the producer and consumer for the task
  std::unique_ptr<flwr::proto::Node> new_producer =
      std::make_unique<flwr::proto::Node>(producer);
  result_task_res.mutable_task()->set_allocated_producer(
      new_producer.release());

  std::unique_ptr<flwr::proto::Node> new_consumer =
      std::make_unique<flwr::proto::Node>(ref_task_ins.task().producer());
  result_task_res.mutable_task()->set_allocated_consumer(
      new_consumer.release());

  // Set ancestry in the task
  result_task_res.mutable_task()->add_ancestry(ref_task_ins.task_id());

  return result_task_res;
}

std::tuple<flwr::proto::ClientMessage, int, bool>
handle(flwr_local::Client *client, flwr::proto::ServerMessage server_msg) {
  if (server_msg.has_reconnect_ins()) {
    std::tuple<flwr::proto::ClientMessage, int> rec =
        _reconnect(server_msg.reconnect_ins());
    return std::make_tuple(std::get<0>(rec), std::get<1>(rec), false);
  }
  if (server_msg.has_get_parameters_ins()) {
    return std::make_tuple(_get_parameters(client), 0, true);
  }
  if (server_msg.has_fit_ins()) {
    return std::make_tuple(_fit(client, server_msg.fit_ins()), 0, true);
  }
  if (server_msg.has_evaluate_ins()) {
    return std::make_tuple(_evaluate(client, server_msg.evaluate_ins()), 0,
                           true);
  }
  throw "Unkown server message";
}

std::tuple<flwr::proto::TaskRes, int, bool>
handle_task(flwr_local::Client *client, const flwr::proto::TaskIns &task_ins) {

#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wdeprecated-declarations"
  if (!task_ins.task().has_legacy_server_message()) {
    // TODO: Handle SecureAggregation
    throw std::runtime_error("Task still needs legacy server message");
  }
  flwr::proto::ServerMessage server_msg =
      task_ins.task().legacy_server_message();
#pragma GCC diagnostic pop

  std::tuple<flwr::proto::ClientMessage, int, bool> legacy_res =
      handle(client, server_msg);
  std::unique_ptr<flwr::proto::ClientMessage> client_message =
      std::make_unique<flwr::proto::ClientMessage>(std::get<0>(legacy_res));

  flwr::proto::TaskRes task_res;
  task_res.set_task_id("");
  task_res.set_group_id("");
  task_res.set_workload_id(0);

  std::unique_ptr<flwr::proto::Task> task =
      std::make_unique<flwr::proto::Task>();

#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wdeprecated-declarations"
  task->set_allocated_legacy_client_message(
      client_message.release()); // Ownership transferred to `task`
#pragma GCC diagnostic pop

  task_res.set_allocated_task(task.release());
  return std::make_tuple(task_res, std::get<1>(legacy_res),
                         std::get<2>(legacy_res));
}
