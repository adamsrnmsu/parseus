// D3 is loaded as a regular static asset by the registers template.
let cached_fc_data;

document.addEventListener("DOMContentLoaded", function() {
    // Select all checkboxes with the class .input-enable and uncheck them
    const checkboxes = document.querySelectorAll('.input-enable[type="checkbox"]');
    checkboxes.forEach(function(checkbox) {
        checkbox.checked = false; // Uncheck the checkbox
        checkbox.parentElement.nextElementSibling.disabled = true;
        checkbox.parentElement.nextElementSibling.required = false;
    });
    // reset the group by form
    document.getElementById("groupby_form").reset();
    // empty out the graph_grouped_by_val
    $("#graph_grouped_by").val("1");
});

// Enable/disable inputs next to a checkbox with input-enable class
$(".input-enable").on("change", event => {
    let siblingInputs = $(event.target).parent().siblings(".form-control");
    siblingInputs.attr("disabled", !event.target.checked);
    siblingInputs.attr("required", event.target.checked);
});

$("#load-data").on("submit", function(event) {
    event.preventDefault();
    event.stopPropagation();
    // Validate inputs
    if (!event.target.checkValidity()) {
        // One or more inputs were invalid
        $(event.target).addClass("was-validated");
        return;
    }
    // Form is valid
    $(event.target).removeClass("was-validated");
    // Disable button and make it a spinner
    let loadDataButton = $("#load-data-submit");
    loadDataButton.attr("disabled", true);
    loadDataButton.html(`
        <span class="spinner-border spinner-border-sm" aria-hidden="true"></span>
        <span role="status">Loading...</span>
    `);

    var completedCalls = 0;
    var totalCalls = 2;
    function checkCompletion() {
        completedCalls++;
        if (completedCalls === totalCalls) {
            // reset button
            loadDataButton.attr("disabled", false);
            loadDataButton.html("<i class='bi-cloud-download me-2'></i>Load Data");
        }
    }

    var registerDataCall = $.ajax({
        method: "GET",
        url: "query",
        data: {
            index: $("#index").val(),
            start: $("#start-time").val(),
            end: $("#end-time").val(),
        },
        timeout: 0,
        success: finishLoadData,
        error: function(jqXHR, textStatus, errorThrown) {
            toastError(`${errorThrown}: ${jqXHR.responseText}`, 10000);
            console.error(`${errorThrown}: ${jqXHR.responseText}`);
        }
    });
    var functionCodeDataCall = $.ajax({
        method: "GET",
        url: "query/fc",
        data: {
            index: $("#index").val(),
            start: $("#start-time").val(),
            end: $("#end-time").val(),
        },
        timeout: 0,
        success: finishLoadData,
        error: function(jqXHR, textStatus, errorThrown) {
            toastError(`${errorThrown}: ${jqXHR.responseText}`, 10000);
            console.error(`${errorThrown}: ${jqXHR.responseText}`);
        }
    });

     // Handle AJAX calls
    registerDataCall.always(checkCompletion);
    functionCodeDataCall.always(checkCompletion);

    // Event listener for the Are you sure you want to leave?
    // **only works after data is loaded
    window.addEventListener('beforeunload', function (e) {
            // Cancel the event and show alert that
            // the unsaved changes would be lost
            e.preventDefault();
    });
});

// click event for name mapping table and select/unselect btns
if (document.getElementById("name-mapping-btn")) {
    const nameMapModal = new bootstrap.Modal("#name-map-modal");
    $("#name-mapping-btn").on("click", event => {
        nameMapModal.show();
    });

    // check registers based on filter
    $("#select-all-btn").on("click", event => {
        $(".register-item").each((_, element) => {
            if (! $(element).hasClass("d-none")) $(element).find('.register-check').first().prop("checked", true );
        });
        // trigger graph change event
        $(".register-check").first().change();
    });
    // uncheck all registers
    $("#unselect-all-btn").on("click", event => {
        document.querySelectorAll('.register-item input[type="checkbox"]').forEach(el => el.checked = false);
        // trigger graph change event
        $(".register-check").first().change();
    });
}

// Filter registers based on checked boxes in filters dropdown
$(".filter-check").on("change", filterRegisters);
function filterRegisters() {
    // Get checked filters
    let filters = $(".filter-check:checked").map((i, element) => {
        return $(element).attr("data-filter");
    }).get();
    // Hide registers that don't match any filters
    $(".register-item").each((_, element) => {
        // Check if register matches any filters
        let show = filters.some(filter => $(element).hasClass(`filter-${filter}`));
        // Show or hide element accordingly
        if (show) $(element).removeClass("d-none");
        else $(element).addClass("d-none");
    });
}

// Filter conversations based on checked boxes in filters dropdown
$(".fc-filter-check").on("change", filterConversations);
function filterConversations() {
    // Get checked filters
    let filters = $(".fc-filter-check:checked").map((i, element) => {
        return $(element).attr("data-filter");
    }).get();

    // Hide conversations that don't match any filters
    $(".fc-conversation-item").each((_, element) => {
        // Check if register matches any filters
        let show = filters.some(filter => $(element).hasClass(`filter-${filter}`));
        // Show or hide element accordingly
        if (show) $(element).removeClass("d-none");
        else $(element).addClass("d-none");
    });
}

function updateResetZoomButton(graph_id) {
    let resetButton = $(`#reset-zoom-${graph_id}`);
    resetButton.toggleClass("disabled", $(".register-check:checked").length == 0);
}

function finishLoadData(data, textStatus, jqXHR) {
    if ("summary" in data) {
        showRegisters(data);
    } else if ("fc_summary" in data) {
        cached_fc_data = data
        showFunctionCodes();
    } else {
        toastError(`Unknown data received.`, 10000);
        console.error(`Unknown data received.`);
    }
}

function getCheckedRegisterInfo() {
    let registers = [];
    $(".register-check:checked").each((_, element) => {
        // Get register element ID
        let registerID = $(element).attr("id");
        // Extract conversation element ID and register name
        let [conversationID, register] = registerID.split("--");
        // Decode conversation name
        let conversation = conversationID.substring(1).replace(/_/g, ".").replace(/-/g, "->");
        // Add info to list
        registers.push([register, conversation]);
    });
    return registers;
}

function addToLegend(legend_id, text, color) {
    // Get legend
    const legend = $(legend_id);
    // Get legend item template
    const legendItemTemplate = $(".legend-item").first();
    // Create new legend item with the given text and color
    let legendItem = legendItemTemplate.clone();
    legendItem.children(".legend-color").css("background-color", color);
    legendItem.children(".legend-text").text(text);
    legendItem.removeClass("d-none");
    // Add element to legend
    legend.append(legendItem);
}

function clearLegend(legend_id) {
    // Remove all elements from the legend (leaving template item)
    $(legend_id).children(".legend-item:not(:first-child)").remove();
}

function normalize(data) {
    let scale = d3.scaleLinear()
        .domain(d3.extent(data, point => point[1]))
        .range([0, 1]);
    return data.map(([x, y]) => [x, scale(y)]);
}

function draw(graph_id, datasets, normalized=false, options={}) {
    // Margins (in pixels)
    options.margin = options.margin ?? {};
    const margin = {
        left: options.margin.left ?? 70,
        right: options.margin.right ?? 20,
        top: options.margin.top ?? 20,
        bottom: options.margin.bottom ?? 120,
    };
    // Drawing values
    const lineWidth = options.lineWidth ?? 3;
    const pointFillOpacity = options.pointFillOpacity ?? 0.25;
    const pointBorderWidth = options.pointBorderWidth ?? 0.5;
    const pointRadius = options.pointRadius ?? 3.5;
    const hoverDotWidth = options.hoverDotWidth ?? pointRadius + pointBorderWidth;
    const div = document.getElementById("nav-tabContent");
    const width = div.clientWidth - 100;
    const height = width * 3/4;
    // Fix datasets
    const fixedDatasets = datasets.map((ds, i) => {
        // Filter out null values
        let dsData = ds.data.filter(([, y]) => y != null);
        // Normalize the data if needed
        if (normalized) dsData = normalize(dsData);
        // Add the dataset index for color values
        dsData = dsData.map(([x, y]) => [x, y, i]);
        // Return updated dataset
        return {...ds, data: dsData};
    });
    // Create data to be graphed
    const data = d3.merge(fixedDatasets.map(ds => ds.data));
    // Create zoom element
    const zoom = d3.zoom()
        .scaleExtent([0.5, 32])
        .on("zoom", zoomed);
    // Create svg
    const svg = d3.create("svg")
        .attr("height", height)
        .attr("width", width)
        .call(zoom);
    // Don't draw points or lines outside of the bounds
    svg.append("clipPath")
        .attr("id", "clipper")
        .append("rect")
            .attr("width", width - (margin.left + margin.right))
            .attr("height", height - (margin.top + margin.bottom))
            .attr("x", margin.left)
            .attr("y", margin.top);
    // Slightly shifted clipPath for highlight text
    svg.append("clipPath")
        .attr("id", "clipper-highlight-text")
        .append("rect")
            .attr("width", width - (margin.left + margin.right) + 12)
            .attr("height", height - (margin.top + margin.bottom) + 14)
            .attr("x", margin.left - 6)
            .attr("y", margin.top - 20);
    // Create scale functions
    const xScale = () => {
        // xScale currently supports types scaleUtc and scaleLinear
        switch ((options.scale ?? {}).x) {
            case "scaleUtc": return d3.scaleUtc();
            case "scaleLinear":
            default: return d3.scaleLinear();
        }
    };
    const yScale = () => {
        // yScale currently only supports scaleLinear
        return d3.scaleLinear();
    };
    // Create domain functions
    const xDomain = () => {
        const [xMin, xMax] = d3.extent(data, d => d[0]);
        // Add padding if requested
        const paddingPercent = (options.padding ?? {}).x ?? 0;
        const padding = paddingPercent * (xMax - xMin);
        return [xMin - padding, Number(xMax) + Number(padding)];
    };
    const yDomain = () => {
        // Min and max are (0, 1) if normalized, else (min, max) of data
        const [yMin, yMax] = normalized ? [0, 1] : d3.extent(data, d => d[1]);
        // Add padding if requested
        const paddingPercent = (options.padding ?? {}).y ?? 0;
        const padding = parseFloat(paddingPercent * (yMax - yMin));
        return [yMin - padding, Number(yMax) + Number(padding)];
    };
    // Create scales (x, y) for position and z for color
    const x = xScale()
        .domain(xDomain())
        .range([margin.left, width - margin.right]);
    const y = yScale()
        .domain(yDomain())
        .range([height - margin.bottom, margin.top]);
    const z = d3.scaleOrdinal()
        .domain(data.map(d => d[2]))
        .range(d3.schemeCategory10);
    // Create axis functions
    const xAxis = (g, x) => g
        .attr("transform", `translate(0, ${height - margin.bottom})`)
        .call(d3.axisBottom(x));
    const yAxis = (g, y) => g
        .attr("transform", `translate(${margin.left},0)`)
        .call(d3.axisLeft(y));
    // Create axis elements
    const gx = svg.append("g")
        .call(xAxis, x);
    const gy = svg.append("g")
        .call(yAxis, y);
    // Add axis labels
    svg.append("text")
        .attr("fill", "white")
        .attr("text-anchor", "middle")
        .attr("x", width / 2)
        .attr("y", height)
        .text(options.xLabel);
    svg.append("text")
        .attr("fill", "white")
        .attr("text-anchor", "middle")
        .attr("x", -height / 2)
        .attr("y", 20)
        .attr("transform", "rotate(-90)")
        .text(options.yLabel);
    // Create gridlines function
    const grid = (g, x, y) => g
        .attr("stroke", "currentColor")
        .attr("stroke-opacity", 0.1)
        .call(g => g
            .selectAll(".x")
            .data(x.ticks())
            .join("line")
                .attr("class", "x")
                .attr("y1", margin.top)
                .attr("y2", height - margin.bottom)
                .attr("x1", d => x(d))
                .attr("x2", d => x(d)))
        .call(g => g
            .selectAll(".y")
            .data(y.ticks())
            .join("line")
                .attr("class", "y")
                .attr("x1", margin.left)
                .attr("x2", width - margin.right)
                .attr("y1", d => y(d))
                .attr("y2", d => y(d)));
    // Add gridlines
    const gGrid = svg.append("g")
        .call(grid, x, y);
    // Add points function
    const points = (g, x, y) => g
        .selectAll("circle")
        .data(data)
        .join("circle")
            .attr("cx", d => x(d[0]))
            .attr("cy", d => y(d[1]))
            .attr("r", pointRadius)
            .attr("clip-path", "url(#clipper)")
            .style("fill", d => z(d[2]))
            .style("fill-opacity", pointFillOpacity)
            .style("stroke", d => z(d[2]))
            .style("stroke-width", pointBorderWidth);
    // Add data points
    const gPoints = svg.append("g")
        .call(points, x, y);
    // Create line function
    const line = (x, y) => d3.line()
        .x(d => x(d[0]))
        .y(d => y(d[1]));
    // Create line drawing function
    const lines = (g, x, y) => g
        .selectAll("path")
        .data(fixedDatasets)
        .join("path")
            .attr("fill", "none")
            .attr("stroke", (_, i) => z(i))
            .attr("stroke-width", lineWidth)
            .attr("clip-path", "url(#clipper)")
            .attr("d", ds => line(x, y)(ds.data))
            .attr("display", ds => ds.showLine ? null : "none");
    // Add lines if enabled
    const gLines = svg.append("g")
        .call(lines, x, y);
    // Create dot drawing function
    const dots = (g, x, y, xm) => g
        .selectAll("g")
        // Get the points closest to the pointer's x position
        .data(fixedDatasets
            .map(ds => ds.data[d3.leastIndex(ds.data, d => Math.abs(x(d[0]) - xm))]))
        // Create a circle and text for each
        .join(
            enter => {
                const g = enter.append("g");
                // add the circle
                g.append("circle")
                    .attr("class", "dot")
                    .attr("r", hoverDotWidth)
                    .attr("fill", "white")
                    .attr("clip-path", "url(#clipper)")
                    .attr("cx", d => x(d[0]))
                    .attr("cy", d => y(d[1]));
                // add the text
                g.append("text")
                    .attr("class", "dot-text")
                    .attr("fill", "white")
                    .attr("text-anchor", "middle")
                    .attr("clip-path", "url(#clipper-highlight-text)")
                    .attr("x", d => x(d[0]))
                    .attr("y", d => y(d[1]) - 8)
                    .text(d => +d[1].toFixed(3));
            },
            update => {
                // update the circle
                update.select("circle")
                    .attr("cx", d => x(d[0]))
                    .attr("cy", d => y(d[1]));
                // update the text
                update.select("text")
                    .attr("x", d => x(d[0]))
                    .attr("y", d => y(d[1]) - 8)
                    .text(d => +d[1].toFixed(3));
            },
        );
    // Create dots element
    const gDots = svg.append("g")
        .attr("display", "none");
    // Create pointer highlighting function
    const highlight = (x, y) => e => {
        // Get pointer location (we only care about x position)
        const [xm, _] = d3.pointer(e);
        // Draw the dot for each dataset at the point nearest to the cursor's x position
        gDots.call(dots, x, y, xm);
    };
    // Enable highlighting on pointer movement
    svg.on("pointermove", highlight(x, y));
    // Show dots when pointer enters graph
    svg.on("pointerenter", () => gDots.attr("display", null));
    // Hide dots when pointer exits graph
    svg.on("pointerleave", () => gDots.attr("display", "none"));
    // Create zooming function
    function zoomed({transform}) {
        const zx = transform.rescaleX(x).interpolate(d3.interpolateRound);
        const zy = transform.rescaleY(y).interpolate(d3.interpolateRound);
        gPoints.call(points, zx, zy);
        gLines.call(lines, zx, zy);
        gx.call(xAxis, zx);
        gy.call(yAxis, zy);
        gGrid.call(grid, zx, zy);
        svg.on("pointermove", highlight(zx, zy));
    }
    // Set zoom reset functionality
    $(`#reset-zoom-${graph_id}`).on("click", () => svg.call(zoom.transform, d3.zoomIdentity));
    return svg;
}

// helper function that's used for the y axis to wrap long phrases
function wrapy(text, width) {
    text.each(function() {
      var text = d3.select(this),
          words = text.text().split(/\s+/).reverse(),
          word,
          line = [],
          lineNumber = 0,
          lineHeight = 1.1, // ems
          x = text.attr("x"),
          dy = parseFloat(text.attr("dy")),
          tspan = text.text(null).append("tspan").attr("x", x).attr("y", 0).attr("dy", dy + "em");

        let len = 0;
      while (word = words.pop()) {
        line.push(word);
        tspan.text(line.join(" "));
        if (tspan.text().length - len > width) {
            line.pop();
            tspan.text(line.join(" "));
            line = [word];
            tspan = text.append("tspan").attr("x", x).attr("y", 0).attr("dy", ++lineNumber * lineHeight + dy + "em").text(word);
            len = tspan.text().length;
        }
      }
    });
  }

function drawHeatmap(graph_id, datasets, _, options={}) {
    // cleanup tooltips
    document.querySelectorAll(".heatmaptooltip").forEach(e => e.remove());

    // Margins (in pixels)
    options.margin = options.margin ?? {};
    const margin = {
        left: options.margin.left ?? 120,
        right: options.margin.right ?? 20,
        top: options.margin.top ?? 10,
        bottom: options.margin.bottom ?? 140,
    };
    // Get graph element and properties
    const div = document.getElementById("analysis-tabContent");
    const width = div.clientWidth - 100;
    const height = width * 3/4;

    // Fix datasets
    const formatTime = d3.utcFormat("%Y-%m-%d %H:%M:%S");
    const fixedDatasets = datasets.map((ds, i) => {
        // Add the dataset index for color values
        const dsData = ds.data.map(([x, y]) => [ds.label, formatTime(x), y]);
        // Return updated dataset
        return {...ds, data: dsData};
    });
    // Create data to be graphed
    const data = d3.merge(fixedDatasets.map(ds => ds.data));

    // Create svg
    const svg = d3.create("svg")
        .attr("height", height)
        .attr("width", width);

    // Create domain functions
    const xDomain = () => data.map(d => d[0]);
    const yDomain = () => data.map(d => d[1]).sort(d3.descending);
    const zDomain = () => d3.extent(data, d => d[2]);

    // situate x scale and add to svg
    const x = d3.scaleBand()
        .domain(xDomain())
        .range([margin.left, width - margin.right])
        .padding(0.005);
    svg.append("g")
        .attr("transform", `translate(0, ${height - margin.bottom})`)
        .call(d3.axisBottom(x))
        
    // situate y scale and add to svg
    const y = d3.scaleBand()
        .domain(yDomain())
        .range([height - margin.bottom, margin.top])
        .padding(0.05);
    svg.append("g")
        .attr("transform", `translate(${margin.left}, 0)`)
        .call(d3.axisLeft(y));

    // create coloring
    const z = d3.scaleSequential()
        .interpolator(d3.interpolateReds)
        .domain(zDomain());

    // create tooltip
    var tooltip = d3.select("#graphdiv")
        .append("div")
        .style("opacity", 0)
        .style("position", "absolute")
        .attr("class", "heatmaptooltip")
        .style("background-color", "white")
        .style("color", "black")
        .style("border", "solid")
        .style("border-color", "black")
        .style("border-width", "1px")
        .style("border-radius", "5px")
        .style("padding", "5px");

    // Three function that change the tooltip when user hover / move / leave a cell
    var mouseover = function(event, d) {
        tooltip
            .style("opacity", 1);
        d3.select(this)
            .style("stroke", "white")
            .style("stroke-width", 2)
            .style("opacity", 1);
    };
    var mousemove = function(event, d) {
        tooltip
            .text(d[2])
            .style("left", (event.pageX + 30) + "px") 
            .style("top", (event.pageY) + "px")
    };
    var mouseleave = function(event, d) {
        tooltip
            .style("opacity", 0);
        d3.select(this)
            .style("stroke", "none")
            .style("opacity", 0.8);
    };

    // add squares
    svg.selectAll()
        .data(data)
        .enter()
        .append("rect")
            .attr("x", d => x(d[0]))
            .attr("y", d => y(d[1]))
            .attr("rx", 4)
            .attr("ry", 4)
            .attr("width", x.bandwidth() )
            .attr("height", y.bandwidth() )
            .style("fill", d => z(d[2]))
            .style("stroke-width", 4)
            .style("stroke", "none")
            .style("opacity", 0.8)
        .on("mouseover", mouseover)
        .on("mousemove", mousemove)
        .on("mouseleave", mouseleave);

    return svg;
}

function drawFcHeatmap(graph_id, datasets, _, options={}) {
    // cleanup tooltips
    document.querySelectorAll(".heatmaptooltip").forEach(e => e.remove());

    // Margins (in pixels)
    options.margin = options.margin ?? {};
    const margin = {
        left: options.margin.left ?? 100,
        right: options.margin.right ?? 80,
        top: options.margin.top ?? 10,
        bottom: options.margin.bottom ?? 150,
    };
    // Get graph element and properties
    const div = document.getElementById("analysis-tabContent");
    const width = div.clientWidth - 100;
    const height = width * 3/4;

    // Fix datasets
    const fixedDatasets = datasets.map((ds, i) => {
        // Add the dataset index for color values
        const dsData = ds.data.map(([x, y]) => [ds.label, x, y]);
        // Return updated dataset
        return {...ds, data: dsData};
    });
    // Create data to be graphed
    const data = d3.merge(fixedDatasets.map(ds => ds.data));

    // Create svg
    const svg = d3.create("svg")
        .attr("height", height)
        .attr("width", width);

    // Create domain functions
    const xDomain = () => data.map(d => d[0]);
    const yDomain = () => data.map(d => d[1]).sort(d3.descending);
    const zDomain = () => d3.extent(data, d => d[2]);

    // situate x scale and add to svg
    const x = d3.scaleBand()
        .domain(xDomain())
        .range([margin.left, width - margin.right])
        .paddingOuter(0.05);
    svg.append("g")
        .attr("transform", `translate(0, ${height - margin.bottom})`)
        .call(d3.axisBottom(x))
            .selectAll("text")
            .style("text-anchor", "end")
            .attr("dx", "-.8em")
            .attr("dy", ".15em")
            .attr("transform", "rotate(-65)");
        
    // situate y scale and add to svg
    const y = d3.scaleBand()
        .domain(yDomain())
        .range([height - margin.bottom, margin.top])
        .padding(0.05);
    svg.append("g")
        .attr("transform", `translate(${margin.left}, 0)`)
        .call(d3.axisLeft(y))
        .selectAll("text")
            .style("align", "center")
            .call(wrapy, "15"); // 15 is characters

    // create coloring
    const z = d3.scaleSequential()
        .interpolator(d3.interpolateReds)
        .domain(zDomain());

    // legend colors, top color and bottom color
    const GradColors = [z(z.domain()[1]), z(z.domain()[0])];

    // scale numbers for legend
    const g_colorScale = svg.append("g")
        .attr("id", "colorLegend");
    var yColorscale = d3.scaleSequential()
        .domain(zDomain())
        .range([height - margin.bottom, margin.top]);
    var y_axis_color = d3.axisRight(yColorscale)
        .ticks(4);
    g_colorScale.append("g")
        .attr("transform", `translate(${width - 35},0)`)
        .call(y_axis_color);


    // enforce linear color scale
     const g_def = g_colorScale.append("defs")
        .append("linearGradient")
            .attr("id", "grad")
            .attr("x1", "0%")
            .attr("x2", "0%")
            .attr("y1", "0%")
            .attr("y2", "100%")  
        .selectAll("stop")
            .data(GradColors)
            .enter()
            .append("stop")
            .style("stop-color", function(d){return d;})
            .attr("offset", function(d,i){
                return 100 * (i / (GradColors.length - 1)) + "%";
            });

    // apply legend color scale to screen
    g_colorScale.append("rect")
        .attr("id", "hm_colorScale_legend")
        .attr("x", width - margin.right + 31)
        .attr("y", margin.top - 2)
        .attr("width", 15)
        .attr("height", height - margin.bottom - 7)
        .attr("stroke", "black")
        .attr("stroke-width", "0.5px")
        .attr("text-anchor", "middle")
        .style("fill", "url(#grad)");

    // create tooltip
    var tooltip = d3.select(`#${graph_id}`)
        .append("div")
        .style("opacity", 0)
        .style("position", "absolute")
        .attr("class", "heatmaptooltip")
        .style("background-color", "white")
        .style("color", "black")
        .style("border", "solid")
        .style("border-color", "black")
        .style("border-width", "1px")
        .style("border-radius", "5px")
        .style("padding", "5px");

    // Three function that change the tooltip when user hover / move / leave a cell
    var mouseover = function(event, d) {
        tooltip
            .style("opacity", 1);
        d3.select(this)
            .style("stroke", "white")
            .style("stroke-width", 2)
            .style("opacity", 1);
    };
    var mousemove = function(event, d) {
        tooltip
            .text(d[2])
            .style("left", (event.pageX + 30) + "px") 
            .style("top", (event.pageY) + "px")
    };
    var mouseleave = function(event, d) {
        tooltip
            .style("opacity", 0);
        d3.select(this)
            .style("stroke", "none")
            .style("opacity", 0.8);
    };

    // add squares
    svg.selectAll()
        .data(data)
        .enter()
        .append("rect")
            .attr("x", d => x(d[0]))
            .attr("y", d => y(d[1]))
            .attr("rx", 0)
            .attr("ry", 0)
            .attr("width", x.bandwidth() )
            .attr("height", y.bandwidth() )
            .style("fill", d => z(d[2]))
            .style("stroke-width", 4)
            .style("stroke", "none")
            .style("opacity", 0.8)
        .on("mouseover", mouseover)
        .on("mousemove", mousemove)
        .on("mouseleave", mouseleave);

    return svg;
}

function drawNodeEdge(graph_id, datasets, _, options={}) {
    // Get graph element and properties
    const div = document.getElementById("nav-tabContent");
    const width = div.clientWidth - 100;
    const height = width * 3/4;

    // fix dataset
    const data = JSON.parse(datasets);

    // create zoom
    const zoom = d3.zoom()
        .extent([[0, 0], [width, height]])
        .scaleExtent([1, 3])
        .on("zoom", zoomed);

    // create svg
    const svg = d3.create("svg")
        .attr("width", width)
        .attr("height", height)
        .attr("viewBox", [-width / 2, -height / 2, width, height]) // keeps it centered
        .call(zoom); // add zoom

    // necessary to use for zoom to work properly
    const g = svg.append("g")
        .attr("cursor", "grab");

    function zoomed({transform}) {
        g.attr("transform", transform);
    }

    // markers are the arrows
    g.append("defs").append("marker")
        .attr("id", "arrow")
        .attr("viewBox", "0 -5 10 10")
        .attr("refX", 15)
        .attr("refY", -1.5)
        .attr("markerWidth", 6)
        .attr("markerHeight", 6)
        .style("opacity", 0.6)
        .attr("fill", "gray")
        .attr("orient", "auto")
    .append("svg:path")
        .attr("d", "M0,-5L10,0L0,5");

    // color scheme for nodes
    var color = d3.scaleOrdinal(d3.schemeTableau10);

    // implements a velocity Verlet numerical integrator for simulating physical forces on particles (nodes)
    var simulation = d3.forceSimulation(data.nodes)
        .force("link", d3.forceLink(data.links).id(d => d.id))
        .force("charge", d3.forceManyBody().strength(-100))
        .force("center", d3.forceCenter()) // keeps the graph in the center
        .force("collision", d3.forceCollide(15)); // keeps the nodes from overlapping

    // links between nodes
    var link = g.append("g")
        .attr("stroke", "gray")
        .attr("stroke-opacity", 0.6)
        .attr("stroke-width", 1)        
        .attr("stroke-linecap",  "round")
    .selectAll("path")
        .data(data.links)
        .attr("source", d=> d.source.id)
        .attr("target", d=> d.target.id)
    .join("path")
        .attr("fill", "none")
        .attr("marker-end", "url(#arrow)");

    // nodes themselves (state_ids)
    var node = g.append("g")
        .attr("stroke", "#fff")
        .attr("stroke-opacity", 1)
        .attr("stroke-width", 1.5)
    .selectAll("circle")
    .data(data.nodes)
    .join("circle")
        .attr("r", 5)
        .attr("fill", function(d) {return color(d.id);})
        .call(d3.drag() // drag is related to clicking and dragging nodes
            .on("start", dragstarted)
            .on("drag", dragged)
            .on("end", dragended));

    // text attached to nodes
    var text = g.append("g").attr("class", "labels").selectAll("g")
        .data(data.nodes)
        .enter().append("g");
    text.append("text")
        .attr("x", 14)
        .attr("y", ".31em")
        .style("font-family", "sans-serif")
        .style("font-size", "0.7em")
        .style("fill", "white")
        .text(function(d) { return d.id; });

    // this function shows each piece on the screen
    function tick() {
        link.attr("d", function(d) {
          var x1 = d.source.x,
              y1 = d.source.y,
              x2 = d.target.x,
              y2 = d.target.y,
    
              // Defaults for normal edge.
              drx = 0,
              dry = 0,
              xRotation = 0, // degrees
              largeArc = 0, // 1 or 0
              sweep = 1; // 1 or 0
    
              // Self edge
              if ( x1 === x2 && y1 === y2 ) {
                // Fiddle with this angle to get loop oriented.
                xRotation = -45;
    
                // Needs to be 1.
                largeArc = 1;
    
                // Smaller number makes smaller loop.
                drx = 10;
                dry = 10;
                
                // For whatever reason the arc collapses to a point if the beginning
                // and ending points of the arc are the same, so kludge it.
                x2 = x2 + 1;
                y2 = y2 + 1;
              } 
    
         return "M" + x1 + "," + y1 + "A" + drx + "," + dry + " " + xRotation + "," + largeArc + "," + sweep + " " + x2 + "," + y2;
        });
        node.attr("transform", function(d) { return "translate(" + d.x + "," + d.y + ")"; });
        text.attr("transform", function(d) { return "translate(" + d.x + "," + d.y + ")"; });
    }

    // add tick to simulation
    simulation.on("tick", tick);

    // dragging nodes functions
    function dragstarted(event, d) {
        if (!event.active) simulation.alphaTarget(0.3).restart();
        d.fx = d.x;
        d.fy = d.y;
    } 
    function dragged(event, d) {
        d.fx = event.x;
        d.fy = event.y;
    }
    function dragended(event, d) {
        if (!event.active) simulation.alphaTarget(0);
        d.fx = null; // comment the x and y lines out to keep the dragged position of a node
        d.fy = null;
    }

    // Set zoom reset functionality
    $(`#reset-zoom-${graph_id}`).on("click", () => svg.call(zoom.transform, d3.zoomIdentity));
    
    return svg
}

function updateGraph(graph_id, drawFunction, datasets, normalized=false, options={}) {
    // Get graph element
    let graph = $(`#${graph_id}`);
    let legend_id = `${graph_id}-legend`
    
    // Remove graph if no registers chosen
    if (datasets.length == 0) {
        graph.children("svg").remove();
        // Make sure legend is removed
        clearLegend(legend_id);
        // Add prompt
        graph.html("<div class='d-flex justify-content-center align-items-center h-100 text-muted'>Please choose a register ...</div>");
        return
    }

    // Create the new graph
    const svg = drawFunction(graph_id, datasets, normalized, options);
    // remove prompt to choose a register
    graph.children("div").remove();
    // Hide chart parent while updating (prevents awkward flashing)
    graph.parent().addClass("d-none");
    // If there is an existing graph, delete it
    graph.children("svg").remove();
    // Add the new graph
    graph.append(svg.node());
    // Show chart parent after updated
    graph.parent().removeClass("d-none");
    // Clear existing legend items
    clearLegend(legend_id);
    // disable/enable zoom reset button for specific graphs
    let resetButton = $(`#reset-zoom-${graph_id}`);
    // Add elements to legend (cases are for special ones)
    switch(drawFunction) {
        case drawHeatmap:
            // no legend
            resetButton.attr("disabled", true);
            break;
        case drawNodeEdge:
            addToLegend(legend_id, "**Each node represents a state_id", null);
            resetButton.attr("disabled", false);
            break;
        default:
            datasets.forEach((ds, i) => addToLegend(legend_id, ds.legendName, d3.schemeCategory10[i]));
            resetButton.attr("disabled", false);
    };
}

function updateFcGraph(graph_id, drawFunction, datasets, normalized=false, options={}) {
    // Get graph element
    let graph = $(`#${graph_id}`);
    // Remove graph if no registers chosen
    if (datasets.length == 0) {
        // Get graph element and remove svg
        graph.children("svg").remove();
        // Add prompt
        graph.html("<div class='d-flex justify-content-center align-items-center h-100 text-muted'>Please choose a register ...</div>");
        return
    }

    // Create the new graph
    const svg = drawFunction(graph_id, datasets, normalized, options);
    // remove prompt to choose a register
    graph.children("div").remove();
    // Hide chart parent while updating (prevents awkward flashing)
    graph.parent().addClass("d-none");
    // If there is an existing graph, delete it
    graph.children("svg").remove();
    // Add the new graph
    graph.append(svg.node());
    // Show chart parent after updated
    graph.parent().removeClass("d-none");
}

function drawValuesOverTime(graph_id, datasets, normalized=false, options={}) {
    // Create datetime parser
    const datetimeParser = d3.utcParse("%Y-%m-%dT%H:%M:%S.%L");
    // Update x values to parsed datetime values
    datasets = datasets.map(ds => {
        let data = ds.data.map(([x, y]) => [datetimeParser(x), y]);
        return {...ds, data: data};
    });
    updateGraph(graph_id, draw, datasets, normalized, options={...options, scale: {x: "scaleUtc"}});
}

function graphRegisters(data, conversations) {
    let datasets = [];
    let registers = [];
    
    // Get checked registers and add them to the datasets
    getCheckedRegisterInfo().forEach(([register, conversation]) => {
        // Get register points
        let points = conversations[conversation][register]["points"];
        // Get short name
        let shortname = conversations[conversation][register]["short_name"];
        // Get line option
        let showLine = $("#show-line").prop("checked");
        // Prevent more than 10 registers from showing since we only have 10 colors
        if (datasets.length < 10) {
            // Add data to datasets
            datasets.push({label: register, data: points, showLine: showLine, legendName: shortname});
            // Add registers to a list
            registers.push(`${conversation}:${register}`);
        }
    });
    // Update the chart
    let shouldNormalize = $("#normalize").prop("checked");
    drawValuesOverTime("register-graph", datasets, shouldNormalize, {
        padding:{
            x: 0.02, 
            y: 0.05
        },
    });
    updateResetZoomButton("register-graph");
    updateStateData();
}

function updateSummary(data) {
    // Update summary info
    $("#timeline").html(data["summary"]["timeline"]["length"]);
    // Update timeline tooltip with start/end times
    bootstrap.Tooltip.getInstance("#timeline").setContent({
        ".tooltip-inner": data["summary"]["timeline"]["start_end"],
    });
    // Update unique conversations count
    $("#conversations").html(data["summary"]["conversations"]);
    // Update unique timestamps count
    $("#timestamps").html(data["summary"]["unique_timestamps"]);
    // Update register count
    $("#counts").html(data["counts"]["total"]);
    // Update register count tooltip with type counts
    bootstrap.Tooltip.getInstance("#counts").setContent({
        ".tooltip-inner": data["counts"]["list"]
    });
}

// for the Summary box (1st box)
function updateFcSummary() {
    // Update summary info
    $("#fc-timeline").html(cached_fc_data["fc_summary"]["total_time"]);
    // Update timeline tooltip with start/end times
    bootstrap.Tooltip.getInstance("#fc-timeline").setContent({
        ".tooltip-inner": cached_fc_data["fc_summary"]["start_end"]
    });
    // Update unique conversations count
    $("#fc-conversations").html(cached_fc_data["fc_summary"]["unique_pairs"]);
    // Update timestamps count
    $("#fc-timestamps").html(cached_fc_data["fc_summary"]["unique_timestamps"]);
    // Update function codes count
    $("#fc-main-summary-count").html(cached_fc_data["fc_summary"]["fc_summary"]);
}

function updateConversationList(data) {
    // Update conversations list
    let conversationList = $("#conversation-list");
    let conversationRegisterLists = $("#conversation-register-lists");

    conversationList.empty();
    conversationRegisterLists.empty();

    let conversationItemTemplate = $("#conversation-item-template");
    let registerListTemplate = $("#register-list-template");
    let registerItemTemplate = $("#register-item-template");
    for (const [conversation, registers] of Object.entries(data["conversations"])) {
        // Create a name that is safe to use as item ID
        let conversationID = "c" + conversation.replace(/\./g, "_").replace(/>/g, "");
        // Clone conversation item template
        let conversationClone = conversationItemTemplate.html();
        let conversationInstance = $(conversationClone);
        // Set conversation ID
        conversationInstance.attr("id", conversationID);
        // Set button text
        conversationInstance.children(".btn").html(conversation.replace(/->/, " → "));
        // Remove d-none so item is shown
        conversationInstance.removeClass("d-none");
        // Add new item to conversation list
        conversationList.append(conversationInstance);
        // Create a register list item for this conversation
        let registerListClone = registerListTemplate.html();
        let registerListInstance = $(registerListClone);
        // Set register list ID
        registerListInstance.attr("id", conversationID + "-list");
        // Add register list to container
        conversationRegisterLists.append(registerListInstance);
        // Add selectable registers to the new item
        for (const [register, properties] of Object.entries(registers)) {
            // Create a name that is safe to use as register ID
            let registerID = conversationID + "--" + register;
            // Clone register list item template
            let registerItemClone = registerItemTemplate.html();
            let registerItemInstance = $(registerItemClone);
            // Add filtering classes
            if (properties["constant"]) registerItemInstance.addClass("filter-constant");
            else registerItemInstance.addClass("filter-variable");
            // Set ID of checkbox
            $(".form-check-input", registerItemInstance).attr("id", registerID);
            // Set checkbox label's for value and text
            let checkLabel = $(".form-check-label", registerItemInstance);
            checkLabel.attr("for", registerID);
            checkLabel.html(register);
            // Remove d-none so register is shown
            registerItemInstance.removeClass("d-none");
            // Add new register item to conversation item
            registerListInstance.append(registerItemInstance);
        }
    }
    // Store conversations
    let conversations = data["conversations"];
    // Set onclick function for conversation buttons
    $(".conversation-item").on("click", event => {
        let conversationID = $(event.currentTarget).attr("id");
        // Remove highlight on all buttons
        $(".conversation-item .btn").removeClass("bg-primary");
        // Add highlight to selected conversation button
        $(event.target).addClass("bg-primary");
        // Hide all register lists
        $(".register-list").addClass("d-none");
        // Show related register list
        $(`#${conversationID}-list`).removeClass("d-none");
    });
    // Set onclick function for modal icons
    $(".register-modal-icon").on("click", event => {
        // Get register element ID
        let registerID = $(event.currentTarget).siblings("input").attr("id");
        // Extract conversation element ID and register name
        let [conversationID, register] = registerID.split("--");
        // Decode conversation name
        let conversation = conversationID.substring(1).replace(/_/g, ".").replace(/-/g, "->");
        // Get register properties
        let properties = conversations[conversation][register];
        // Set values in modal
        $(`#register-name`).html(conversation + ":" + register);
        for (const [name, value] of Object.entries(properties)) {
            $(`#register-${name}`).html(value != null ? value.toString() : "N/A");
        }
    });
    // Filter registers based on initial settings
    filterRegisters();
    return conversations;
}

// updates the conversation list
function updateFcConversationList() {
    // Update conversations list
    let conversationList = $("#fc-conversation-list");
    let conversationSummary = $("#fc-conversation-summary");

    conversationList.empty();
    conversationSummary.empty();
    
    let conversationItemTemplate = $("#fc-conversation-item-template");
    let summaryItemTemplate = $("#summary-item-template");

    for (const [conversation, d] of Object.entries(cached_fc_data["conversation_list"])) {
        // Create a name that is safe to use as item ID
        let conversationID = "c" + conversation.replace(/\./g, "_").replace(/ -> /g, "-");
        // Clone conversation item template
        let conversationClone = conversationItemTemplate.html();
        let conversationInstance = $(conversationClone);
        // Set conversation ID
        conversationInstance.attr("id", conversationID);
        // Set button text
        conversationInstance.children(".btn").html(conversation.replace(/->/, "→"));
        // Remove d-none so item is shown
        conversationInstance.removeClass("d-none");
        // Add protocol classes
        for (let [_, proto] of Object.entries(d["protocols"])) {
            conversationInstance.addClass(`filter-${proto}`);
        }
        // Add new item to conversation list
        conversationList.append(conversationInstance);

        // Create a summary item for this conversation
        let summaryClone = summaryItemTemplate.html();
        let summaryInstance = $(summaryClone);
        // Set summary ID
        summaryInstance.attr("id", conversationID + "-summary");
        // Add summary to container
        conversationSummary.append(summaryInstance);
    }

    updateFcConversationSummary();

    // Set onclick function for conversation buttons
    $(".fc-conversation-item").on("click", event => {
        let conversationID = $(event.currentTarget).attr("id");

        // Remove highlight on all buttons
        $(".fc-conversation-item .btn").removeClass("bg-primary");
        // Add highlight to selected conversation button
        $(event.target).addClass("bg-primary");
        
        // Hide all summaries
        $(".summary-item").addClass("d-none");
        // Show related summary
        $(`#${conversationID}-summary`).removeClass("d-none");

        // graph the data
        graphConvo(event.currentTarget)
    });
    // Filter conversations based on initial settings
    filterConversations();
}

function updateFcConversationSummary() {
    const fc_conversation_items = document.querySelectorAll('.fc-conversation-item');
    fc_conversation_items.forEach(item => {
        const button = item.querySelector('button');
        if (button) {
            let conversation = button.textContent.replace(" → ", " -> ");
            let d = cached_fc_data["conversation_list"][conversation];
            let conversationSummaryID = item.id + "-summary";
            let summaryInstance = document.getElementById(conversationSummaryID);
            let summary_html = `
            <h6 class="card-title text-center py-2">Summary stats</h6>
            <ul class="list-group list-group-flush">
                <li class="list-group-item d-flex justify-content-between small">
                    <span>First seen:</span>
                    <span>${d["stats"]["start_time"]}</span>
                </li>
                <li class="list-group-item d-flex justify-content-between small">
                    <span>Last seen:</span>
                    <span>${d["stats"]["end_time"]}</span>
                </li>
                <li class="list-group-item d-flex justify-content-between small">
                    <span>Total time:</span>
                    <span>${d["stats"]["total_time"]}</span>
                </li>
                <li class="list-group-item d-flex justify-content-between small">
                    <span>Function codes:</span>
                    <pre>${d["stats"]["fc_summary"]}</pre>
            </li>
            </ul>
            `
            summaryInstance.innerHTML = summary_html;
        }
    })
}

function showRegisters(data) {
    // Update summary stats
    updateSummary(data);
    // Update conversation list
    let conversations = updateConversationList(data);
    // Set check functionality for register checkboxes and chart options
    $(".register-check, .chart-option").on("change", () => graphRegisters(data, conversations));
    $(window).on("resize", () => updateStateData());
    // Create initial graph
    graphRegisters(data, conversations);
    // Create name map table
    populateTable(data);
}

function showFunctionCodes() {
    // Update summary stats
    updateFcSummary();
    // Update conversation list
    updateFcConversationList();
    // Create initial graph
    graphConvo(null);
}

// Pulls out the correct conversation from the data list and reformats it. Sends data to graph drawer. 
function graphConvo(element) {
    let groupby_btn = $("#groupby_btn");
    // empty graph initially
    if (element == null) {
        // disable group by btn
        groupby_btn.attr("disabled", true);
        // update graph as empty
        updateFcGraph("fc-graph", drawFcHeatmap, []);
        return
    }

    // pull variables
    let conversations = cached_fc_data["conversation_list"]
    let convo_key = element.innerText.replace("→", "->");
    let convo_data = conversations[convo_key];

    // reformat data
    const data = Object.entries(convo_data["points"]).map(([k, v]) => ({
        label: k, data: Object.keys(v).map((key) => [key, v[key]])
    }));

    // enable group by button
    groupby_btn.attr("disabled", false);

    // update the graph
    updateFcGraph("fc-graph", drawFcHeatmap, data);
}

function populateTable(data) {
    // Separate heading from rows
    const [headings, ...rows] = data["name_map_array"];
    
    // Create table
    let table = `
      <h6>Short to Long Mapping</h6>
      <table class="table table-striped table-bordered">
        <thead class="">${getCells(headings, 'th')}</thead>
        <tbody>${createBody(rows)}</tbody>
      </table>
    `;
    // add the table
    $("#short-to-long-modal-body").html(table);
}

function getCells(data, type) {
    return data.map(cell => `<${type}>${cell}</${type}>`).join('');
}

function createBody(data) {
    return data.map(row => `<tr>${getCells(row, 'td')}</tr>`).join('');
}

function graphRegisterState(data) {
    if ("state_id" in data["states"])
        drawValuesOverTime("state-graph", [{label: "State ID", data: data["states"]["state_id"], showLine: false, legendName: "State ID"}], false, {
            padding:{
                x: 0.01, 
                y: 0.05
            },
        });
    else
        drawValuesOverTime("state-graph", []);
    updateResetZoomButton("state-graph");
}

function graphRegisterHeatmap(data) {
    // Create datetime parser
    const datetimeParser = d3.utcParse("%Y-%m-%dT%H:%M:%S");
    const datasets = Object.entries(data["heatmap"]).map(([k, v]) => ({
        label: k, data: v.map(([t, c]) => [datetimeParser(t), c])
    }));
    updateGraph("state-graph", drawHeatmap, datasets);
    updateResetZoomButton("state-graph");
}

function graphNodeEdge(data) {
    if (Object.keys(data["node_edge"]).length != 0){
        updateGraph("state-graph", drawNodeEdge, data["node_edge"]);
    }
    else {
        updateGraph("state-graph", drawNodeEdge, []);
    }
    updateResetZoomButton("state-graph");
}

function updateStateGraph(data) {
    // Get selected graph type
    let graphType = $("#state-graph-select option:checked").val();
    switch(graphType) {
        case "Register State Over Time":
            graphRegisterState(data);
            break;
        case "Register Updates Heatmap":
            graphRegisterHeatmap(data);
            break;
        case "State Transition Graph":
            graphNodeEdge(data);
            break;
        default:
            console.error(`Unknown graph type: ${graphType}`);
    }
}

function updateStateData() {
    let registers = getCheckedRegisterInfo().map(([register, conversation]) => {
        return `${conversation}:${register}`;
    });
    // Get state data from the server
    $.ajax({
        method: "GET",
        url: "state/state/",
        data: {
            index: $("#index").val(),
            start: $("#start-time").val(),
            end: $("#end-time").val(),
            registers: registers,
            graph: $("#state-graph-select option:checked").val(),
        },
        timeout: 10000,
        success: (data, textStatus, jqXHR) => {
            updateStateGraph(data);
        },
        error: (response) => {
            toastError(`${response.status} ${response.statusText} Error: ${response.responseText}`, 10000);
            console.error(`${response.status} ${response.statusText} Error: ${response.responseText}`);
            drawValuesOverTime("state-graph", []);
        }
    });
}


// Set onclick function for regrouping the data
$("#groupby_form").on("submit", event => {
    event.preventDefault();
    // get the data and form the graph
    groupby_ajax(`${$("#numGroupby").val()}${$("#granularityGroupby").val()}`);
});

function groupby_ajax(groupby) {
    $.ajax({
        method: "GET",
        url: "query/fc",
        data: {
            index: $("#index").val(),
            start: $("#start-time").val(),
            end: $("#end-time").val(),
            groupby: groupby,
        },
        timeout: 10000,
        success: (data, textStatus, jqXHR) => {
            let element = $(".fc-conversation-item button.bg-primary");
            if (element.length > 0) {
                // update the graph_grouped_by val
                $("#graph_grouped_by").val(`${groupby}`);
                // graph the data
                cached_fc_data = data;
                updateFcConversationSummary();
                graphConvo(element[0].parentElement);
            }
        },
        error: (response) => {
            toastError(`${response.status} Error: ${response.responseText}`, 10000);
            console.error(`${response.status} Error: ${response.responseText}`);
            updateFcGraph('fc-graph', drawFcHeatmap, []);
        }
    });
}