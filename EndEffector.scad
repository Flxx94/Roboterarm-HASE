module profileConnector(){
    difference() {
        translate([0,0,25]) cube([25,25,50], center = true);
        translate([0,0,25]) cube([20,20,50], center = true);
    }
}

module connectorconnector() {
    translate([0,0,52.5]) cube([40,40,5], center = true);
}

profileConnector();
connectorconnector();