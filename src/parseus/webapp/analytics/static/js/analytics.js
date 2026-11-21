const analytics = {
  analytic_modal: function (id) {
    let analyticModal = new bootstrap.Modal($("#" + id));
    analyticModal.show();
  },

  // helper function to uncheck all boxes
  uncheckAll: function () {
    document
      .querySelectorAll('input[type="checkbox"]')
      .forEach((el) => (el.checked = false));
  },

  update_analytic_settings: function (event) {
    event.preventDefault();

    // gather form data
    var form = new FormData(event.target);
    let headers = { "X-Csrftoken": $("input[name=csrfmiddlewaretoken]").val() };
    let analyticpk = form.get("analyticpk");
    const formObject = {};
    form.forEach((value, key) => {
      formObject[key] = value;
    });
    // send request
    $.ajax({
      type: "POST",
      url: `/analytics/analytic_user_input/${analyticpk}`,
      data: formObject,
      headers: headers,
      success: function (response, textStatus, xhr) {
        toastSuccess("Successfully updated analytic's settings", 7000);

        // dismiss the modal we dont have an ID of (its too dynamic) by triggering the Cancel button
        event.target.lastElementChild.firstElementChild.click();
      },
      error: function (response, textStatus, exception) {
        toastError(`${response.status} Error: ${response.responseText}`, 10000);
      },
    });
  },

  confirm_delete_model: function () {
    let model = $("#delete-model-modal input[name=delete-this-model]").val();
    let pk = $("#delete-model-modal input[name=delete-trained-model-pk]").val();
    let headers = { "X-Csrftoken": $("input[name=csrfmiddlewaretoken]").val() };
    $.ajax({
      type: "GET",
      url: `/analytics/train_model/delete/${pk}/`,
      headers: headers,
      success: function (response, textStatus, xhr) {
        // request went through
        deleteModal.hide();
        document.getElementById(`trained-model-${pk}-line`).remove();
        toastSuccess(`Successfully deleted ${model}`, 7000);
      },
      error: function (response) {
        // request failed
        deleteModal.hide();
        toastError(`${response.status} Error: ${response.responseText}`, 10000);
        console.error(`${response.status} Error: ${response.responseText}`);
      },
    });
  },

  push_results: function () {
    let url = "/analytics/push_results/";
    const databox = document.getElementById("data_box");
    const outidx = document.getElementById("outgoing_idx");
    let jsondata = databox.querySelector("code").textContent;
    let outgoing_idx = outidx.value;
    let data = {
      es_data: jsondata,
      outidx: outgoing_idx,
      csrfmiddlewaretoken: $("input[name=csrfmiddlewaretoken]").val(),
    };

    // show spinner
    $("#spinner").removeClass("invisible");

    $.ajax({
      type: "POST",
      url: url,
      data: data,
      success: function (response) {
        // remove spinner if it was showing
        $("#spinner").addClass("invisible");
        toastSuccess("Successfully pushed results to elastic", 7000);
      },
      error: function (response) {
        // remove spinner if it was showing
        $("#spinner").addClass("invisible");
        toastError(`${response.status} Error: ${response.responseText}`, 10000);
        console.error(`${response.status} Error: ${response.responseText}`);
      },
    });
  },

  // helper function for reusing this bit of code
  add_on_model_click_btn: function () {
    $(".model-btn").on("click", (event) => {
      // deselect previously selected message
      $(".model-btn").removeClass("active");
      // get newly selected message element
      let model = $(event.target);
      // add active class so it appears selected
      model.addClass("active");
    });
  },
};

// analytic settings : submission
$("form#analytic-ui-form").on("submit", analytics.update_analytic_settings);

// delete trained model modal: submission
$("#confirm-delete-model").on("click", analytics.confirm_delete_model);

// analytics page: push results
$("#push_results_btn").on("click", analytics.push_results);

window.analytics = analytics;
